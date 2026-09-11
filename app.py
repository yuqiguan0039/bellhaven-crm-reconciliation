#!/usr/bin/env python3
"""Bellhaven website-to-CRM reconciliation pipeline and human review app."""
from __future__ import annotations

import argparse, hashlib, html, json, os, re, sqlite3, ssl, sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BASE = "https://analyst-assessment-production.up.railway.app"
API = BASE + "/api/v1"
ROOT = Path(__file__).resolve().parent
DB = ROOT / "review.db"
PARENT_NAME = "Bellhaven Senior Living (Parent Account)"

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def norm(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("saint", "st"))
def norm_street(s):
    s = (s or "").lower()
    replacements={"northwest":"nw","northeast":"ne","southwest":"sw","southeast":"se","street":"st","avenue":"ave","road":"rd","boulevard":"blvd","drive":"dr","lane":"ln","north":"n","south":"s","east":"e","west":"w","center":"ctr","centre":"ctr"}
    for a,b in replacements.items(): s=re.sub(rf"\b{a}\b",b,s)
    return norm(s)
def care_value(vals):
    mapped=[]
    for v in vals:
        v={"Short-Term Rehabilitation & Nursing":"Skilled Nursing","Memory Support":"Memory Care"}.get(v,v)
        if v not in mapped: mapped.append(v)
    return "; ".join(mapped)
def requires_chow(account):
    return account.get("lifetime_revenue",0)>0 and account.get("outstanding_ar",0)>0

def fetch(path, token=None, method="GET", body=None):
    headers={"User-Agent":"BellhavenCRMReconciler/1.0","Accept":"application/json,text/html"}
    if token: headers["Authorization"]="Bearer "+token
    data=None
    if body is not None:
        data=json.dumps(body).encode(); headers["Content-Type"]="application/json"
    req=Request(urljoin(BASE,path),data=data,headers=headers,method=method)
    # The python.org macOS build does not always inherit Keychain roots.
    context=ssl.create_default_context(cafile="/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").exists() else None)
    with urlopen(req,timeout=30,context=context) as r:
        raw=r.read(); c=r.headers.get("content-type","")
        return json.loads(raw) if "json" in c else raw.decode()

def scrape():
    links=[]
    for page in range(1,10):
        text=fetch(f"/communities?page={page}")
        found=re.findall(r'<h3><a href="([^"]+)">',text)
        for x in found:
            if x not in links: links.append(x)
        if f"Page {page} /" not in text or "Next &rarr;" not in text: break
    rows=[]
    for path in links:
        text=fetch(path)
        name=html.unescape(re.search(r"<h1>(.*?)</h1>",text,re.S).group(1)).strip()
        address=re.search(r"<dt>Address</dt><dd>(.*?)<br>(.*?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)</dd>",text,re.S)
        cares=[html.unescape(x).strip() for x in re.findall(r'<dt>Care Offerings</dt><dd>(.*?)</dd>',text,re.S) for x in re.findall(r'<span class="badge">(.*?)</span>',x,re.S)]
        phone_m=re.search(r"<dt>Phone</dt><dd>(.*?)</dd>",text,re.S)
        rows.append({"name":name,"street":html.unescape(address.group(1)).strip(),"city":html.unescape(address.group(2)).strip(),"state":address.group(3),"zip":address.group(4),"care_offerings":cares,"phone":html.unescape(phone_m.group(1)).strip() if phone_m else "","source_url":urljoin(BASE,path)})
    return rows

def crm_accounts(token): return fetch("/api/v1/accounts?page=1&page_size=200",token)["data"]
def fingerprint(kind, account_id, proposed, source_key):
    raw=json.dumps([kind,account_id,proposed,source_key],sort_keys=True,separators=(",",":"))
    return hashlib.sha256(raw.encode()).hexdigest()

def init_db():
    con=sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS proposals(
      fingerprint TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
      account_id TEXT, website_json TEXT, crm_json TEXT, proposed_json TEXT NOT NULL,
      evidence TEXT NOT NULL, confidence REAL NOT NULL, decision TEXT NOT NULL DEFAULT 'Pending',
      created_at TEXT NOT NULL, decided_at TEXT, applied_at TEXT, error TEXT)""")
    con.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS run_history(started_at TEXT NOT NULL, completed_at TEXT, website_locations INTEGER, crm_accounts INTEGER, proposals_found INTEGER, new_proposals INTEGER, error TEXT)")
    default_mode="test" if os.environ.get("TEST_MODE","").lower() in ("1","true","yes") else "live"
    con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('mode',?)",(default_mode,))
    con.commit(); return con

def get_mode(con):
    row=con.execute("SELECT value FROM settings WHERE key='mode'").fetchone()
    return row[0] if row else "test"

def match_score(w,a):
    street=norm_street(w["street"])==norm_street(a.get("billing_street"))
    phone=norm(w.get("phone"))==norm(a.get("phone")) and bool(norm(w.get("phone")))
    zipmatch=w["zip"][:5]==(a.get("billing_zip") or "")[:5]
    city=norm(w["city"])==norm(a.get("billing_city")) and w["state"]==a.get("billing_state")
    name=SequenceMatcher(None,norm(w["name"]),norm(a.get("name"))).ratio()
    score=(0.52 if street else 0)+(0.18 if phone else 0)+(0.13 if zipmatch else 0)+(0.08 if city else 0)+0.09*name
    return score,{"street_exact":street,"phone_exact":phone,"zip_exact":zipmatch,"city_state_exact":city,"name_similarity":round(name,3)}

def generate(token):
    history=init_db(); started=now()
    history.execute("INSERT INTO run_history(started_at) VALUES(?)",(started,)); run_id=history.execute("SELECT last_insert_rowid()").fetchone()[0]; history.commit()
    try: web= scrape(); accounts=crm_accounts(token)
    except Exception as exc:
        history.execute("UPDATE run_history SET completed_at=?,error=? WHERE rowid=?",(now(),str(exc),run_id)); history.commit(); history.close(); raise
    parent=next(a for a in accounts if a["name"]==PARENT_NAME)
    matches={}; used=set(); proposals=[]
    for w in web:
        ranked=sorted(((match_score(w,a)[0],a,match_score(w,a)[1]) for a in accounts if a["account_id"]!=parent["account_id"]),key=lambda x:x[0],reverse=True)
        score,a,signals=ranked[0]
        # A verified website phone plus matching ZIP and city/state is sufficient
        # when the street is stale (PO boxes and abbreviation drift are common).
        identity_ok=score>=.67 or (signals["phone_exact"] and signals["zip_exact"] and signals["city_state_exact"])
        if identity_ok:
            matches[w["source_url"]]=a["account_id"]; used.add(a["account_id"])
            changes={}
            expected={"name":w["name"],"parent_id":parent["account_id"],"billing_street":w["street"],"billing_city":w["city"],"billing_state":w["state"],"billing_zip":w["zip"],"care_type":care_value(w["care_offerings"]),"status":"Active"}
            for k,v in expected.items():
                current=a.get(k) or ""
                same=norm_street(current)==norm_street(v) if k=="billing_street" else current==v
                if not same: changes[k]=v
            if changes:
                if "parent_id" in changes and requires_chow(a):
                    new_account={**expected,"phone":w["phone"],"note":f"Current account created for CHOW from {a['account_id']}; source {w['source_url']}"}
                    chow={"old_parent_id":a.get("parent_id","") ,"new_account":new_account}
                    proposals.append(("chow",f"CHOW {a['name']}",a,w,chow,f"Revenue {a['lifetime_revenue']} and outstanding AR {a['outstanding_ar']} require preserving the old account. {signals}; source {w['source_url']}",score))
                else:
                    proposals.append(("update",f"Update {a['name']}",a,w,changes,f"Website exact/near match: {signals}; source {w['source_url']}",score))
        else:
            payload={"name":w["name"],"parent_id":parent["account_id"],"billing_street":w["street"],"billing_city":w["city"],"billing_state":w["state"],"billing_zip":w["zip"],"care_type":care_value(w["care_offerings"]),"status":"Active","phone":w["phone"],"note":f"Created from Bellhaven website: {w['source_url']}"}
            proposals.append(("create",f"Create {w['name']}",None,w,payload,f"No CRM match above threshold; best score {score:.3f}. Source {w['source_url']}",1-score))
    # Duplicate exact-address accounts: choose higher-revenue/older-looking record as survivor.
    groups={}
    for a in accounts:
        key=(norm_street(a.get("billing_street")),a.get("billing_zip"))
        if key[0] and key[1]: groups.setdefault(key,[]).append(a)
    duplicate_losers=set()
    for group in groups.values():
        if len(group)<2 or any(a.get("chow_current_account") for a in group): continue
        bh=[a for a in group if a["account_id"] in used]
        if not bh: continue
        # The account selected by the current website match must survive an
        # ownership/rebrand collision. Revenue is only a tie-breaker.
        survivor=max(bh,key=lambda a:(a.get("lifetime_revenue",0),a.get("outstanding_ar",0)))
        for loser in group:
            if loser["account_id"]==survivor["account_id"] or loser.get("duplicate_of_account"): continue
            changes={"duplicate_of_account":survivor["account_id"],"status":"Inactive","note":f"Duplicate of {survivor['account_id']} ({survivor['name']}); exact normalized address and ZIP; reviewed {now()[:10]}."}
            proposals.append(("duplicate",f"Deactivate duplicate {loser['name']}",loser,None,changes,f"Exact normalized address+ZIP match with survivor {survivor['account_id']}; survivor chosen by revenue/AR continuity.",.99))
            duplicate_losers.add(loser["account_id"])
    # Current Bellhaven children absent from website and not already matched.
    for a in accounts:
        if a.get("parent_id")==parent["account_id"] and a["account_id"] not in used and a["account_id"] not in duplicate_losers and not a.get("duplicate_of_account"):
            changes={"status":"Needs Review","note":f"Not present in Bellhaven website directory on {now()[:10]}; ownership/removal requires review."}
            proposals.append(("missing_website",f"Review former Bellhaven account: {a['name']}",a,None,changes,"Account is under Bellhaven parent but no current website location matched.",.90))
    con=init_db(); inserted=0
    for kind,title,a,w,changes,evidence,confidence in proposals:
        aid=a["account_id"] if a else ""
        source=w["source_url"] if w else "website-directory-absence"
        fp=fingerprint(kind,aid,changes,source)
        cur=con.execute("INSERT OR IGNORE INTO proposals VALUES(?,?,?,?,?,?,?,?,?,'Pending',?,NULL,NULL,NULL)",(fp,kind,title,aid,json.dumps(w or {}),json.dumps(a or {}),json.dumps(changes),evidence,confidence,now()))
        inserted+=cur.rowcount
    con.commit(); con.close()
    (ROOT/"data").mkdir(exist_ok=True)
    (ROOT/"data"/"website_locations.json").write_text(json.dumps(web,indent=2),encoding="utf-8")
    (ROOT/"data"/"crm_snapshot.json").write_text(json.dumps(accounts,indent=2),encoding="utf-8")
    history.execute("UPDATE run_history SET completed_at=?,website_locations=?,crm_accounts=?,proposals_found=?,new_proposals=? WHERE rowid=?",(now(),len(web),len(accounts),len(proposals),inserted,run_id)); history.commit(); history.close()
    print(json.dumps({"website_locations":len(web),"crm_accounts":len(accounts),"proposals_found":len(proposals),"new_proposals":inserted},indent=2))

def apply_one(con, fp, token):
    row=con.execute("SELECT kind,account_id,proposed_json,decision,applied_at FROM proposals WHERE fingerprint=?",(fp,)).fetchone()
    if not row or row[3]!="Approved" or row[4]: return
    kind,aid,raw,_,_=row; payload=json.loads(raw)
    try:
        if kind=="create": result=fetch("/api/v1/accounts",token,"POST",payload)
        elif kind=="chow":
            current=fetch(f"/api/v1/accounts/{aid}",token)
            if current.get("chow_current_account"):
                new_id=current["chow_current_account"]
            else:
                created=fetch("/api/v1/accounts",token,"POST",payload["new_account"])
                new_id=created["account_id"]
            result=fetch(f"/api/v1/accounts/{aid}",token,"PATCH",{"parent_id":payload["old_parent_id"],"chow_current_account":new_id})
        else:
            current=fetch(f"/api/v1/accounts/{aid}",token)
            if all((current.get(k) or "")==v for k,v in payload.items()): result=current
            else: result=fetch(f"/api/v1/accounts/{aid}",token,"PATCH",payload)
        con.execute("UPDATE proposals SET applied_at=?,error=NULL WHERE fingerprint=?",(now(),fp)); con.commit(); return result
    except Exception as e:
        con.execute("UPDATE proposals SET error=? WHERE fingerprint=?",(str(e),fp)); con.commit(); raise

def page(con):
    writes_enabled=os.environ.get("ALLOW_CRM_WRITES","").lower() in ("1","true","yes")
    test_mode=not writes_enabled or get_mode(con)=="test"
    rows=con.execute("SELECT fingerprint,kind,title,website_json,crm_json,proposed_json,evidence,confidence,decision,applied_at,error FROM proposals ORDER BY decision='Pending' DESC, kind,title").fetchall()
    cards={k:[] for k in ("update","create","chow","duplicate","missing_website")}
    completed=[]; rejected=[]
    labels={"name":"Facility Name","parent_id":"Parent Account ID","billing_street":"Street Address","billing_city":"City","billing_state":"State","billing_zip":"ZIP Code","care_type":"Care Offerings","status":"Status","phone":"Phone","duplicate_of_account":"Duplicate Of Account","chow_current_account":"CHOW Current Account","note":"Note"}
    web_keys={"name":"name","billing_street":"street","billing_city":"city","billing_state":"state","billing_zip":"zip","phone":"phone"}
    def show(v):
        if isinstance(v,list): return ", ".join(str(x) for x in v)
        if isinstance(v,dict): return json.dumps(v,ensure_ascii=False)
        return str(v) if v not in (None,"") else "—"
    for r in rows:
        fp,kind,title,w,c,p,e,conf,d,applied,err=r
        website,crm,proposed=json.loads(w),json.loads(c),json.loads(p)
        if kind=="chow":
            proposed_flat=proposed.get("new_account",{})
            proposed_flat["chow_current_account"]="New account ID assigned after creation"
        else: proposed_flat=proposed
        fields=[]
        ordered=["name","parent_id","billing_street","billing_city","billing_state","billing_zip","care_type","phone","status","duplicate_of_account","chow_current_account","note"]
        for key in ordered:
            wk=web_keys.get(key)
            webval=website.get(wk,"") if wk else (", ".join(website.get("care_offerings",[])) if key=="care_type" else "")
            old=crm.get(key,""); new=proposed_flat.get(key,old)
            if key not in proposed_flat and not webval and not old: continue
            changed=key in proposed_flat and show(old)!=show(new)
            fields.append(f'''<tr class="{'changed' if changed else ''}"><th>{labels.get(key,key)}</th><td>{html.escape(show(old))}</td><td>{html.escape(show(webval))}</td><td>{html.escape(show(new))}</td><td>{'Change' if changed else 'Keep'}</td></tr>''')
        source=website.get("source_url","")
        source_link=f'<a href="{html.escape(source)}" target="_blank">View website evidence ↗</a>' if source else "Website directory absence check"
        financial=""
        if crm:
            financial=f'''<div class="facts"><span>Account ID: <b>{html.escape(show(crm.get('account_id')))}</b></span><span>Lifetime Revenue: <b>${crm.get('lifetime_revenue',0):,.0f}</b></span><span>Outstanding AR: <b>${crm.get('outstanding_ar',0):,.0f}</b></span></div>'''
        action_names={"create":"Create Account","update":"Update Account","duplicate":"Deactivate Duplicate","missing_website":"Flag for Review","chow":"Create and Link CHOW Account"}
        approve_label="Simulate Approval" if test_mode else "Approve and Apply"
        reject_label="Simulate Rejection" if test_mode else "Reject"
        is_pending=d=="Pending"
        controls_html=f'''<form method="post" action="/decide"><input type="hidden" name="fp" value="{fp}"><button name="decision" value="Approved">{approve_label}</button><button class="reject" name="decision" value="Rejected">{reject_label}</button></form>''' if is_pending else ""
        result_text=('Applied to CRM: '+applied) if applied else ('Reviewed in simulation — CRM unchanged' if d.startswith('Test ') else d)
        card=f'''<article><header><span class="pill {d.replace(' ','')}">{html.escape('Applied' if applied else d)}</span><b>{html.escape(title)}</b><span class="action">{action_names.get(kind,kind)}</span><small>Match confidence: {conf:.0%}</small></header>{financial}<div class="evidence"><b>Supporting evidence:</b> {html.escape(e)} · {source_link}</div><div class="tablewrap"><table><thead><tr><th>Field</th><th>Current CRM Value</th><th>Website Value</th><th>Proposed Value</th><th>Action</th></tr></thead><tbody>{''.join(fields)}</tbody></table></div>{'<p class="err">'+html.escape(err)+'</p>' if err else ''}{controls_html}<small>{html.escape(result_text)}</small></article>'''
        if is_pending: cards.setdefault(kind,[]).append(card)
        elif d in ("Rejected","Test Rejected"): rejected.append(card)
        else: completed.append(card)
    counts=dict(con.execute("SELECT decision,count(*) FROM proposals GROUP BY decision"))
    banner='<div class="testbanner"><b>READ-ONLY REVIEW MODE — CRM WRITES DISABLED</b><br>Refresh only reads current data. Decision buttons are simulations and never call POST or PATCH.</div>' if test_mode else '<div class="livebanner"><b>LIVE MODE</b> — Approved changes are written to the CRM API.</div>'
    last=con.execute("SELECT completed_at,error FROM run_history ORDER BY rowid DESC LIMIT 1").fetchone()
    tz=ZoneInfo("America/Chicago"); local_now=datetime.now(tz); next_run=local_now.replace(hour=7,minute=0,second=0,microsecond=0)
    if next_run<=local_now: next_run+=timedelta(days=1)
    if last:
        last_text=datetime.fromisoformat(last[0]).astimezone(tz).strftime("%b %d, %Y at %I:%M %p CT")
    elif (ROOT/"data"/"crm_snapshot.json").exists():
        last_text=datetime.fromtimestamp((ROOT/"data"/"crm_snapshot.json").stat().st_mtime,tz).strftime("%b %d, %Y at %I:%M %p CT")
    else: last_text="Never"
    next_text=next_run.strftime("%b %d, %Y at %I:%M %p CT")
    controls=f'''<div class="controls"><div><span>Last refreshed</span><b>{last_text}</b></div><div><span>Next scheduled refresh</span><b>{next_text}</b></div><form method="post" action="/refresh"><button>↻ Refresh Now</button></form></div>'''
    section_names={"update":"Update Existing Accounts","create":"Create New Accounts","chow":"Change of Ownership (CHOW)","duplicate":"Deactivate Duplicate Accounts","missing_website":"Missing from Website — Needs Review"}
    nav=''.join(f'<a href="#{k}">{section_names[k]} <b>{len(cards[k])}</b></a>' for k in section_names)+f'<a href="#completed">Completed / Reviewed <b>{len(completed)}</b></a><a href="#rejected">Rejected <b>{len(rejected)}</b></a>'
    sections=''.join(f'<section id="{k}"><h2>{section_names[k]} <span>{len(cards[k])}</span></h2>{"".join(cards[k]) if cards[k] else "<p class=empty>No proposals in this category.</p>"}</section>' for k in section_names)
    sections+=f'<section id="completed"><h2>Completed / Reviewed Changes <span>{len(completed)}</span></h2>{"".join(completed) if completed else "<p class=empty>No completed changes yet.</p>"}</section>'
    sections+=f'<section id="rejected"><h2>Rejected Proposals <span>{len(rejected)}</span></h2>{"".join(rejected) if rejected else "<p class=empty>No rejected proposals.</p>"}</section>'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Bellhaven CRM Review</title><style>body{{font:14px system-ui;margin:0;background:#f4f6f8;color:#263238}}main{{max-width:1200px;margin:auto;padding:32px}}h1{{margin:0 0 6px;color:#234d43}}h2{{margin-top:34px;border-bottom:2px solid #315f52;padding-bottom:8px}}h2 span{{font-size:13px;background:#315f52;color:white;border-radius:12px;padding:3px 9px}}nav{{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}}nav a{{background:white;border:1px solid #cdd7d3;padding:8px 11px;border-radius:6px;text-decoration:none}}.controls{{display:flex;align-items:center;gap:22px;flex-wrap:wrap;background:white;border:1px solid #d8dee3;padding:14px 16px;border-radius:9px;margin:16px 0}}.controls div{{display:grid;gap:3px}}.controls span{{font-size:11px;text-transform:uppercase;color:#6d7a80}}.controls form{{margin-left:auto}}.testbanner,.livebanner{{padding:14px 18px;border-radius:8px;margin:16px 0}}.testbanner{{background:#fff0bd;border:2px solid #d89b00;color:#704c00}}.livebanner{{background:#d9eee4;border:1px solid #76aa91}}article{{background:white;border:1px solid #d8dee3;border-radius:9px;padding:20px;margin:18px 0;box-shadow:0 2px 8px #0000000a}}header{{display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:16px}}header small{{margin-left:auto;color:#607078}}.pill,.action{{padding:4px 10px;border-radius:12px;background:#e9eef1;font-size:12px}}.Pending{{background:#fff0bd}}.Approved,.Applied,.TestApproved{{background:#ccebd7}}.Rejected,.TestRejected{{background:#f3d0d0}}.facts{{display:flex;gap:26px;flex-wrap:wrap;background:#f7f9fa;padding:10px 12px;margin:14px 0}}.evidence{{margin:12px 0;line-height:1.6}}a{{color:#176b57}}.tablewrap{{overflow:auto;border:1px solid #ccd5da;margin:14px 0}}table{{border-collapse:collapse;width:100%;min-width:820px}}th,td{{border-right:1px solid #dbe2e6;border-bottom:1px solid #dbe2e6;padding:9px 11px;text-align:left;vertical-align:top}}thead th{{background:#315f52;color:white;position:sticky;top:0}}tbody th{{background:#eef3f1;white-space:nowrap}}tr.changed td{{background:#fff8dc}}tr.changed td:last-child{{color:#9a6100;font-weight:700}}button{{padding:9px 14px;background:#2e5d50;color:white;border:0;border-radius:5px;margin:4px 8px 8px 0;cursor:pointer}}button.reject{{background:#8c4242}}.empty{{background:white;padding:16px;border:1px dashed #bbc7c2}}.err{{color:#a00}}</style></head><body><main><h1>Bellhaven CRM Review</h1>{controls}{banner}<p><b>{counts.get('Pending',0)} proposals need review</b> · {len(completed)} completed/reviewed · {len(rejected)} rejected</p><nav>{nav}</nav>{sections}</main></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        con=init_db(); content=page(con).encode(); con.close(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.end_headers(); self.wfile.write(content)
    def do_POST(self):
        n=int(self.headers.get("Content-Length",0)); form=parse_qs(self.rfile.read(n).decode())
        if self.path=="/refresh":
            generate(os.environ["CRM_API_TOKEN"]); self.send_response(303); self.send_header("Location","/"); self.end_headers(); return
        if self.path=="/mode":
            mode=form.get("mode",["test"])[0]
            if mode not in ("test","live"): mode="test"
            con=init_db(); con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('mode',?)",(mode,)); con.commit(); con.close()
            self.send_response(303); self.send_header("Location","/"); self.end_headers(); return
        fp=form["fp"][0]; decision=form["decision"][0]; con=init_db()
        writes_enabled=os.environ.get("ALLOW_CRM_WRITES","").lower() in ("1","true","yes")
        test_mode=not writes_enabled or get_mode(con)=="test"
        if test_mode: decision="Test "+decision
        con.execute("UPDATE proposals SET decision=?,decided_at=? WHERE fingerprint=? AND decision IN ('Pending','Test Approved','Test Rejected')",(decision,now(),fp)); con.commit()
        if decision=="Approved": apply_one(con,fp,os.environ["CRM_API_TOKEN"])
        con.close(); self.send_response(303); self.send_header("Location","/"); self.end_headers()
    def log_message(self,fmt,*args): pass

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    sub.add_parser("run"); serve=sub.add_parser("serve"); serve.add_argument("--port",type=int,default=8080)
    decide=sub.add_parser("decide"); decide.add_argument("decision",choices=["approve","reject"]); decide.add_argument("fingerprints",nargs="+")
    args=ap.parse_args(); token=os.environ.get("CRM_API_TOKEN")
    if args.cmd=="run":
        if not token: sys.exit("CRM_API_TOKEN is required")
        generate(token)
    elif args.cmd=="serve":
        if not token: sys.exit("CRM_API_TOKEN is required")
        print(f"Review app: http://127.0.0.1:{args.port}"); ThreadingHTTPServer(("127.0.0.1",args.port),Handler).serve_forever()
    else:
        if not token: sys.exit("CRM_API_TOKEN is required")
        con=init_db()
        for fp in args.fingerprints:
            d="Approved" if args.decision=="approve" else "Rejected"; con.execute("UPDATE proposals SET decision=?,decided_at=? WHERE fingerprint=? AND decision='Pending'",(d,now(),fp)); con.commit()
            if d=="Approved": apply_one(con,fp,token)
        con.close()
if __name__=="__main__": main()
