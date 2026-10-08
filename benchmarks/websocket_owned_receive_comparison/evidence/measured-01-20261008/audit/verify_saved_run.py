#!/usr/bin/env python3
"""Read-only verifier for the saved owned-segment comparison. Never runs the campaign."""
import hashlib, itertools, json, math, random, shutil, statistics, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINS = {
 "PLAN.md":"6f24c457250c58ed54920946019d4e13e4bcc93320e820ab363616160463f3da",
 "run_owned_comparison.py":"ff84f2b390c2dbb85a516610ccbd44f30aa4b8c5347acc845053c416248dc0fe",
 "test_owned_campaign.py":"d8e8d36ef892bd863f1af129767ee6d1b73f2988ed3de6e1d655913bb1a1b3c9",
 "source-pins.json":"5f891e15575c3390e71f9e84ced837697ee46453570858d74afdfffc80f557f7",
 "timing/status.json":"4e2f2611633e0adfdec5fdb83026f1052188e0d648baaee6beecd3dd66c23112",
 "timing/manifest.json":"a6052afe5d7acc7d743111e66ec9cfef23ce975b9e4ca09a711f861d48c83ca7",
 "timing/report.json":"6c677572d88d92ab82bd7e519ecdc801d0a4c881104ca0349b7167183f9da57d",
 "timing/attempts.jsonl":"6791858dfb5e3b62ee1791c07b9644a176eca4e3792d559871ff70f43f54cf1d",
 "timing/command.txt":"293c1b66f30866804a98ef137e7101dcecc13e53f64f30fdb339cf04fa70a893",
 "timing/terminal.log":"b5154632f9ea97228a7d970e56123de9d6107fef84017ae83a63fd10be32b459",
 "smoke/status.json":"fdfd491e5bbdfa92f8ece8e0d9fd40dbc67c67fa49c47e24b96885054efd3b35",
 "smoke/manifest.json":"f242ab80f457c91071720b209555d98393ae8b95822ae252008e5daa96259763",
 "smoke/report.json":"fa4b31f818247a88a0a5a76b75ce3b1c58fdee4b520ab43265396324db2de3f2",
 "smoke/attempts.jsonl":"ab306b6f8f98dce69b5a34ec1b19ca31ca39bcc55be7caf68f5890ed4ab393c0",
 "backend-build-manifest.json":"e5fe238c5d4782cb47beaae811d26e5649b27823cbfa145ca7b715f788dbde4f",
}
DSO = ("/home/baidu/scrapanium-experiments/wss-owned-segments-20261007/backend-prefix-01/lib/libcurl-impersonate.so.4.8.0",
       "2a0be84b6dc33d023b2b748163bc49a32352502e189d23012defcdb29167a439")
V=("baseline","candidate","curl_cffi")
SW=[("stream-30b-flush1",30,65536,1),("stream-1024b-flush1",1024,16384,1),
 ("stream-65536b-flush1",65536,1024,1),("stream-30b-flush64",30,65536,64),
 ("stream-1024b-flush64",1024,16384,64),("stream-65536b-flush64",65536,1024,64)]
RT=[("original-python-30b-c1",30,1000,1,"python"),("go-30b-c1",30,5000,1,"go"),
 ("go-1024b-c1",1024,2000,1,"go"),("go-65536b-c1",65536,300,1,"go"),
 ("go-30b-c4",30,5000,4,"go"),("go-1024b-c4",1024,2000,4,"go"),
 ("go-65536b-c4",65536,300,4,"go")]
class AuditError(RuntimeError): pass
def req(x,m):
 if not x: raise AuditError(m)
def sha(p):
 h=hashlib.sha256()
 with Path(p).open("rb") as f:
  for b in iter(lambda:f.read(1048576),b""): h.update(b)
 return h.hexdigest()
def js(p):
 try:return json.loads(Path(p).read_text(encoding="utf-8"))
 except Exception as e: raise AuditError(f"invalid JSON {p}: {e}") from e
def jl(p):
 try:
  xs=[json.loads(x) for x in Path(p).read_text(encoding="utf-8").splitlines()]
  req(all(xs),"empty JSONL row in "+str(p)); return xs
 except Exception as e:
  if isinstance(e,AuditError):raise
  raise AuditError(f"invalid JSONL {p}: {e}") from e
def close(a,b,m):
 req(isinstance(a,(int,float)) and math.isfinite(a) and math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-12),f"{m}: expected {b}, got {a}")
def sumcheck(root):
 p=root/"SHA256SUMS"; req(p.is_file(),"missing SHA256SUMS"); exp={}
 entries=list(root.rglob("*"))
 req(not root.is_symlink() and not any(x.is_symlink() for x in entries),"package contains a symlink")
 base=root.resolve()
 for n,line in enumerate(p.read_text(encoding="ascii").splitlines(),1):
  z=line.split("  ",1); req(len(z)==2 and len(z[0])==64,f"bad checksum line {n}")
  h,name=z; r=Path(name); req(not r.is_absolute() and ".." not in r.parts and name!="SHA256SUMS","unsafe checksum path")
  target=root/r
  req(base in target.resolve().parents,"checksum path escapes package")
  req(name not in exp,"duplicate checksum path"); exp[name]=h
 actual={x.relative_to(root).as_posix() for x in entries if x.is_file() and x!=p}
 req(actual==set(exp),"checksum file list mismatch")
 for name,h in exp.items():req(sha(root/name)==h,"checksum mismatch: "+name)
 return len(exp)
def orders(seed,runs,names):
 rng=random.Random(seed); perms=list(itertools.permutations(V)); out={}
 for name in names:
  block=[]
  for _ in range((runs+5)//6):
   p=perms.copy(); rng.shuffle(p); block.extend(p)
  out[name]=block[:runs]
 return out
def expected_row_identities(manifest,runs,seed):
 sw=[x["name"] for x in manifest["workloads"]["stream"]]
 rw=[x["name"] for x in manifest["workloads"]["roundtrip"]]
 rng=random.Random(seed);rng.shuffle(sw);rng.shuffle(rw)
 so=orders(seed+1,runs,[x["name"] for x in manifest["workloads"]["stream"]])
 ro=orders(seed+2,runs,[x["name"] for x in manifest["workloads"]["roundtrip"]])
 result=[]
 for fault in ("corrupt","swap"):
  for pos,v in enumerate(V):
   result.append(("stream-control",f"negative-{fault}",0,v,pos,f"negative-{fault}-{v}"))
 for phase,names,table in (("stream",sw,so),("roundtrip",rw,ro)):
  for repeat in range(runs):
   shift=repeat%len(names)
   for name in names[shift:]+names[:shift]:
    for pos,v in enumerate(table[name][repeat]):
     result.append((phase,name,repeat,v,pos,f"{name}-r{repeat:03d}-{v}"))
 return result
def check_row_order(rows,manifest,runs,seed,label):
 actual=[(x["phase"],x["workload"],x["repeat"],x["variant"],x["variant_order_index"],x["run_id"]) for x in rows]
 req(actual==expected_row_identities(manifest,runs,seed),label+" JSONL schedule/order mismatch")
def boot(vals,seed):
 rng=random.Random(seed)
 s=sorted(statistics.median(rng.choices(vals,k=len(vals))) for _ in range(10000))
 return [s[249],s[9749]]
def ci(actual,vals,seed,label):
 req(len(actual)==2,label+" CI shape"); exp=boot(vals,seed)
 close(actual[0],exp[0],label+" CI low");close(actual[1],exp[1],label+" CI high")
def matrices(m, smoke=False):
 sw=[{"name":n,"bytes":b,"count":32 if smoke else c,"connections":1,"peer_flush_frames":f} for n,b,c,f in SW]
 rt=[{"name":n,"bytes":b,"count":20 if smoke else c,"connections":k,"peer":p} for n,b,c,k,p in RT]
 req(m["workloads"]["stream"]==sw,"stream matrix changed")
 req(m["workloads"]["roundtrip"]==rt,"roundtrip matrix changed")
 return {x["name"]:x for x in sw},{x["name"]:x for x in rt}
def sourcecheck(root,pins):
 ch=pins["candidate"]["changed_file_sha256"]; sr=root/"candidate-source"
 actual={x.relative_to(sr).as_posix() for x in sr.rglob("*") if x.is_file()}
 req(actual==set(ch),"candidate source path map mismatch")
 for n,h in ch.items():req(sha(sr/n)==h,"candidate source mismatch: "+n)
 return len(ch)
def buildcheck(root,key,m,s):
 rec=m["build_artifacts"];req(len(rec)==10 and s["successful_build_count"]==10,key+" build count")
 names=set()
 for r in rec:
  n=r["name"];req(n not in names,key+" duplicate build"); names.add(n)
  req(r["returncode"]==0,key+" failed build "+n)
  p=root/key/"build-logs"/(n+".json")
  req(p.is_file() and sha(p)==r["log_sha256"],key+" build log hash "+n)
  x=js(p);req(x["name"]==n and x["returncode"]==0 and x["command"]==r["command"] and x["cwd"]==r["cwd"],key+" build log fields "+n)
 links=[x for x in rec if x["name"].endswith("-link")];req(len(links)==4,key+" link count")
 for r in links:req(all(x in r["command"] for x in ("-std=c11","-O3","-g","-ldl","-lcurl-impersonate")),key+" link flags "+r["name"])
 for n,k in (("binary_sha256",6),("generated_c_sha256",4)):
  d=m[n];req(len(d)==k and all(len(x)==64 for x in d.values()),key+" "+n)
def backend(row,m,label):
 r=row["result"]; maps=[]
 if "mapped_backend" in r:maps.append(r["mapped_backend"])
 maps+=r.get("mapped_backends",[])
 req(bool(maps),label+" no mapped DSO")
 b=m["backend"];req((b["path"],b["sha256"],b["tls_backend"])==(DSO[0],DSO[1],"BoringSSL"),label+" backend pin")
 req(all((x.get("path"),x.get("sha256"))==(DSO[0],DSO[1]) for x in maps),label+" runtime DSO mismatch")
def streamcheck(row,w,m,label,smoke=False):
 r=row["result"];p=r["peer_observation"];n=32 if smoke else w["count"]; payload=n*w["bytes"]
 if not smoke:
  close(r["expected_corpus_payload_bytes"],payload,label+" corpus")
  req(r["clock_sanity"]["client_interval_within_parent"] is True,label+" clock gate")
  close(r["elapsed_ms"],r["end_ms"]-r["start_ms"],label+" interval")
 req(p["starts"]==1 and p["warmups"]==1 and p["data_frames"]==n,label+" frames/starts")
 req(p["data_payload_bytes"]==payload and p["data_frame_bytes"]==payload+n*hdr(w["bytes"],False),label+" frame bytes")
 req((p["tls_version"],p["tls_cipher"])==(772,4865),label+" Go TLS")
 backend(row,m,label)
def hdr(n,masked):
 return (2 if n<=125 else 4 if n<=65535 else 10)+(4 if masked else 0)
def rtcheck(row,w,m,label,smoke=False):
 r=row["result"];n=(20 if smoke else w["count"])+1; payload=n*w["bytes"];p=r["peer_observation"]
 if w["peer"]=="python":
  req(p["frames"]==n and p["payload_bytes"]==payload and p["opcode_fin_payload_match"] is True,label+" Python frames/payload")
  req(len(p["tls_connections"])==w["connections"],label+" Python TLS count")
  for x in p["tls_connections"]:req((x["tls_version"],x["tls_cipher"],x["tls_cipher_bits"])==("TLSv1.3","TLS_AES_256_GCM_SHA384",256),label+" Python TLS")
 else:
  req(len(p)==w["connections"],label+" Go conn count")
  for x in p:
   req(x["frames"]==n and x["payload_bytes"]==payload,label+" Go frames/payload")
   req(x["client_frame_bytes"]==payload+n*hdr(w["bytes"],True) and x["server_frame_bytes"]==payload+n*hdr(w["bytes"],False),label+" Go framed bytes")
   req((x["tls_version"],x["tls_cipher"])==(772,4865),label+" Go TLS")
  if not smoke:req(len(r["intervals"])==w["connections"],label+" Go interval count")
 backend(row,m,label)
def smokecheck(root):
 d=root/"smoke";m=js(d/"manifest.json");s=js(d/"status.json");r=js(d/"report.json");rows=jl(d/"attempts.jsonl")
 req((s["inputs"]["source_pins_sha256"],m["runner_sha256"],m["plan_sha256"])==(PINS["source-pins.json"],PINS["run_owned_comparison.py"],PINS["PLAN.md"]),"smoke pins")
 req(s["status"]=="complete" and s["successful_attempts"]==45 and s["builds_started"] and s["attempts_started"],"smoke status")
 req(r["status"]=="correctness_smoke_complete" and r["smoke_only"] is True and r["performance_claims"] is False and r["acceptance_claims"] is False and r["all_correctness_checks_passed"] is True,"smoke claims/status")
 req(r["reduced_counts"]=={"roundtrip_each":20,"stream_each":32} and len(rows)==45,"smoke counts")
 sw,rw=matrices(m,smoke=True);controls=[x for x in rows if x["phase"]=="stream-control"];positive=[x for x in rows if x["phase"] in ("stream","roundtrip")]
 req(len(controls)==6 and len(positive)==39 and all(x["status"]=="ok" for x in rows),"smoke row status")
 forbidden={"elapsed_ms","start_ms","end_ms","messages_per_second","round_trips_per_second","rate_per_second",
            "rate_median_bootstrap_95_ci","paired_ratios","bootstrap_95_ci","targets_met"}
 def has_timing(value):
  if isinstance(value,dict):return any(k in forbidden or has_timing(v) for k,v in value.items())
  if isinstance(value,list):return any(has_timing(v) for v in value)
  return False
 req(not has_timing(r) and not any(has_timing(x.get("result",{})) for x in rows),
     "smoke contains timing/performance result fields")
 check_row_order(rows,m,1,20261008,"smoke")
 seen=set()
 for x in positive:
  k=(x["phase"],x["workload"],x["variant"]);req(k not in seen and x["repeat"]==0 and x["variant"] in V,"smoke duplicate/identity");seen.add(k)
  req(x["run_id"]==f"{x['workload']}-r000-{x['variant']}","smoke run ID")
  (streamcheck if x["phase"]=="stream" else rtcheck)(x,(sw if x["phase"]=="stream" else rw)[x["workload"]],m,str(k),True)
 req(len(seen)==39,"smoke matrix incomplete")
 ctrl={(x["result"]["fault"],x["variant"]) for x in controls}
 req(ctrl=={(f,v) for f in ("corrupt","swap") for v in V},"smoke controls matrix")
 for x in controls:
  req(x["result"]["detected_by_exact_check"] is True and x["result"]["returncode"]==1,"smoke control outcome")
  backend(x,m,"smoke control")
 buildcheck(root,"smoke",m,s);return len(rows)
def timingcheck(root):
 d=root/"timing";m=js(d/"manifest.json");s=js(d/"status.json");report=js(d/"report.json");rows=jl(d/"attempts.jsonl")
 req(s["status"]=="complete" and s["successful_attempts"]==474 and s["successful_build_count"]==10 and s["builds_started"] and s["attempts_started"],"timing status")
 req((m["mode"],m["seed"],m["runs_per_workload_variant"],m["smoke_reduced_counts"])==("performance_comparison",20261008,12,None),"timing options")
 req((s["inputs"]["source_pins_sha256"],m["runner_sha256"],m["plan_sha256"])==(PINS["source-pins.json"],PINS["run_owned_comparison.py"],PINS["PLAN.md"]),"timing pins")
 req(report["status"]=="complete" and len(rows)==474,"timing report/count")
 sw,rw=matrices(m);pos=[x for x in rows if x["phase"] in ("stream","roundtrip")];ctrl=[x for x in rows if x["phase"]=="stream-control"]
 req(len(pos)==468 and len(ctrl)==6 and all(x["status"]=="ok" for x in rows),"timing row totals/status")
 seen=set();groups={}
 for x in pos:
  ph,name,v,rep=x["phase"],x["workload"],x["variant"],x["repeat"];mat=sw if ph=="stream" else rw
  req(name in mat and v in V and 0<=rep<12,"timing unknown workload/repeat")
  k=(ph,name,v,rep);req(k not in seen,"duplicate timing row");seen.add(k)
  groups.setdefault((ph,name,v),[]).append(x)
  req(x["run_id"]==f"{name}-r{rep:03d}-{v}","timing run id")
  (streamcheck if ph=="stream" else rtcheck)(x,mat[name],m,str(k))
 expected={(ph,n,v,i) for ph,mat in (("stream",sw),("roundtrip",rw)) for n in mat for v in V for i in range(12)}
 req(seen==expected,"timing fixed matrix incomplete")
 wants={(f,v) for f in ("corrupt","swap") for v in V};got={(x["result"].get("fault"),x["variant"]) for x in ctrl}
 req(got==wants,"timing control matrix")
 for x in ctrl:
  q=x["result"];req(x["repeat"]==0 and q["detected_by_exact_check"] is True and q["returncode"]==1,"control acceptance")
  req((q["peer_observation"]["tls_version"],q["peer_observation"]["tls_cipher"])==(772,4865),"control TLS")
  backend(x,m,"control")
 so=orders(20261009,12,list(sw));ro=orders(20261010,12,list(rw))
 for table in (so,ro):
  for n,blocks in table.items():
   for start in (0,6):req(set(blocks[start:start+6])==set(itertools.permutations(V)),n+" not balanced")
 for x in pos:
  table=so if x["phase"]=="stream" else ro
  req(x["variant"]==table[x["workload"]][x["repeat"]][x["variant_order_index"]],"variant order mismatch")
 check_row_order(rows,m,12,20261008,"timing")
 checked=0
 for ph,mat,summaries,base in (("stream",sw,report["stream"],20261008),("roundtrip",rw,report["roundtrip"],20261009)):
  req(len(summaries)==len(mat),ph+" summary count")
  for wi,w in enumerate(mat.values()):
   srow=summaries[wi];name=w["name"];req(srow["workload"]==w,ph+" summary workload")
   times={}
   for v in V:
    rr=sorted(groups[(ph,name,v)],key=lambda q:q["repeat"]);ts=[q["result"]["elapsed_ms"] for q in rr];times[v]=ts
    vm=srow["variants"][v];req(vm["elapsed_ms"]==ts,ph+" raw elapsed mismatch")
    close(vm["median_ms"],statistics.median(ts),ph+" median")
    count=w["count"]*w.get("connections",1);rates=[count*1000/t for t in ts]
    close(vm["rate_per_second"],statistics.median(rates),ph+" rate")
    ci(vm["rate_median_bootstrap_95_ci"],rates,base+wi,ph+" rate")
   specs=(("candidate_vs_baseline","candidate","baseline"),("baseline_vs_curl_cffi","baseline","curl_cffi"),("candidate_vs_curl_cffi","candidate","curl_cffi"))
   for ri,(key,num,den) in enumerate(specs):
    vals=[times[den][i]/times[num][i] for i in range(12)];a=srow["paired_ratios"][key]
    req(len(a["samples"])==12,ph+" ratio n")
    for aa,bb in zip(a["samples"],vals):close(aa,bb,ph+" ratio sample")
    close(a["median"],statistics.median(vals),ph+" ratio median")
    ci(a["bootstrap_95_ci"],vals,base+wi+ri,ph+" "+key);checked+=1
 sg={q["workload"]["name"]:q["paired_ratios"]["candidate_vs_baseline"]["bootstrap_95_ci"][0]>=.95 for q in report["stream"]}
 rg={q["workload"]["name"]:q["paired_ratios"]["candidate_vs_baseline"]["bootstrap_95_ci"][0]>=.95 for q in report["roundtrip"]}
 req(report["stream_non_regression"]==sg and sum(sg.values())==0,"stream gates")
 req(report["roundtrip_non_regression"]==rg and sum(rg.values())==2,"round-trip gates")
 tg={q["workload"]["name"]:q["paired_ratios"]["candidate_vs_curl_cffi"]["bootstrap_95_ci"][0]>=2 for q in report["stream"] if q["workload"]["bytes"]==65536}
 req(report["targets_met"]==tg and sum(tg.values())==0,"large stream targets")
 buildcheck(root,"timing",m,s)
 return len(rows),checked
def historycheck(root,pins):
 a=js(root/"history/campaign-01/status.json")
 req(a["status"]=="failed" and a["builds_started"] is False and a["attempts_started"] is False,"campaign01 history")
 req(not (root/"history/campaign-02/status.json").exists() and not (root/"history/campaign-02/timing").exists(),"campaign02 invented output")
 p=js(root/"validation/preflight-03/status.json")
 req(p["status"]=="preflight_complete" and p["preflight_only"] is True and not p["builds_started"] and not p["attempts_started"],"final preflight")
 require_helpers = p["baseline_helper_imports"]["helpers"]
 req(p["python_cache_was_empty_before_helpers"] is True and len(require_helpers)==5,"helper provenance")
 baseline=Path(pins["baseline"]["path"])
 for run in ("smoke","timing"):
  m=js(root/run/"manifest.json")
  observed=m["baseline_helper_imports"]["helpers"]
  req(set(observed)==set(require_helpers),run+" helper inventory")
  prefix=Path(m["baseline_helper_imports"]["cache_prefix"])
  for name,entry in observed.items():
   ref=require_helpers[name]
   req(entry["source_path"]==ref["source_path"] and entry["source_sha256"]==ref["source_sha256"] and entry["cache_sha256"]==ref["cache_sha256"],run+" helper identity "+name)
   rel=Path(entry["source_path"]).relative_to(baseline).as_posix()
   req(pins["baseline"]["source_sha256"].get(rel)==entry["source_sha256"],run+" helper source pin "+name)
   cp=Path(entry["cache_path"])
   req(cp.is_relative_to(prefix) and len(entry["cache_sha256"])==64,run+" helper bytecode provenance "+name)
def verify(root=ROOT):
 n=sumcheck(root)
 for rel,h in PINS.items():req((root/rel).is_file() and sha(root/rel)==h,"frozen pin "+rel)
 req((root/"PLAN.md").read_text(encoding="utf-8").isascii(),"plan not ASCII")
 pins=js(root/"source-pins.json");req(sourcecheck(root,pins)==32,"snapshot count")
 req(sha(root/"backend-build-manifest.json")==PINS["backend-build-manifest.json"],"backend build manifest")
 smoke=smokecheck(root);timing,ratios=timingcheck(root);historycheck(root,pins)
 return n,smoke,timing,ratios
def rewrite_sums(root):
 rows=[]
 for p in sorted(x for x in root.rglob("*") if x.is_file() and x.name!="SHA256SUMS"):
  rows.append(f"{sha(p)}  {p.relative_to(root).as_posix()}")
 (root/"SHA256SUMS").write_text("\n".join(rows)+"\n",encoding="ascii")
def expect_failure(action,fragment,label):
 try:action()
 except AuditError as exc:
  req(fragment in str(exc),label+" failed for wrong reason: "+str(exc))
  return str(exc)
 raise AuditError(label+" unexpectedly passed")
def selftest():
 result=verify(ROOT)
 with tempfile.TemporaryDirectory(prefix="owned-evidence-audit-") as tmp:
  d=Path(tmp)/"missing-row";shutil.copytree(ROOT,d)
  p=d/"timing/attempts.jsonl"
  p.write_text("\n".join(p.read_text(encoding="utf-8").splitlines()[:-1])+"\n",encoding="utf-8")
  rewrite_sums(d)
  expect_failure(lambda:verify(d),"frozen pin timing/attempts.jsonl","missing-row integrity")
  expect_failure(lambda:timingcheck(d),"timing report/count","missing-row semantics")
  d=Path(tmp)/"reordered-row";shutil.copytree(ROOT,d)
  p=d/"timing/attempts.jsonl";lines=p.read_text(encoding="utf-8").splitlines()
  lines[6],lines[7]=lines[7],lines[6];p.write_text("\n".join(lines)+"\n",encoding="utf-8");rewrite_sums(d)
  expect_failure(lambda:timingcheck(d),"JSONL schedule/order mismatch","reordered-row semantics")
  d=Path(tmp)/"missing-source";shutil.copytree(ROOT,d)
  (d/"candidate-source/native/websocket.inc.c").unlink();rewrite_sums(d)
  pins=js(d/"source-pins.json")
  expect_failure(lambda:sourcecheck(d,pins),"candidate source path map mismatch","missing-source semantics")
  d=Path(tmp)/"modified-source";shutil.copytree(ROOT,d)
  q=d/"candidate-source/native/websocket.inc.c";q.write_bytes(q.read_bytes()+b"\n");rewrite_sums(d)
  pins=js(d/"source-pins.json")
  expect_failure(lambda:sourcecheck(d,pins),"candidate source mismatch: native/websocket.inc.c","modified-source semantics")
 return result
def main():
 import argparse
 a=argparse.ArgumentParser(description=__doc__);a.add_argument("--self-test",action="store_true");args=a.parse_args()
 n,sm,ti,ra=selftest() if args.self_test else verify(ROOT)
 print(f"PASS checksum_files={n} candidate_source_snapshots=32")
 print(f"PASS correctness_smoke_attempts={sm} performance_claims=false")
 print(f"PASS timing_attempts={ti} paired_ratio_series={ra} bootstrap_resamples=10000")
 print("PASS gates=stream 0/6 round_trip 2/7 large_64KiB 0/2")
 if args.self_test:
  print("PASS self_test=missing_row_integrity_and_semantics rejected")
  print("PASS self_test=reordered_schedule rejected")
  print("PASS self_test=missing_and_modified_source_snapshots rejected")
if __name__=="__main__":
 try:main()
 except AuditError as e:raise SystemExit("FAIL: "+str(e))
