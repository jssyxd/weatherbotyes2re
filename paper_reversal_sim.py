#!/usr/bin/env python3
"""Standalone paper simulator for weatherbotyes2re."""
from __future__ import annotations
import argparse, json, time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from re_execution import paper_match_fak, plan_fire_cycle, size_legs
from reversal_strategy import maybe_arm_or_fire, ensure_re_state
TZ = "Asia/Shanghai"

def make_buckets():
    out = []
    for t in range(28, 36):
        out.append({"bucket_id": f"h{t}", "lo": float(t), "hi": float(t+1), "no_token_id": f"NO-{t}", "yes_token_id": f"YES-{t}"})
    return out

def make_city():
    return {"city_id": "shanghai", "icao": "ZSPD", "timezone": TZ}

def make_book(ask, depth=8.0, tick=0.01, levels=4):
    asks = []
    px = Decimal(str(ask)); step = Decimal(str(tick)); sz = Decimal(str(depth))
    for i in range(levels):
        asks.append({"price": str(px + step*i), "size": str(sz/(i+1))})
    return {"best_ask": asks[0]["price"], "tick_size": str(tick), "asks": asks}

def apply_scramble(books, broken_no, new_yes, step):
    no_book = books.get(broken_no)
    if no_book and no_book.get("best_ask"):
        books[broken_no] = make_book(min(float(no_book["best_ask"])+0.04*step, 0.90), depth=max(1.0, 6.0-step*2))
    if new_yes and new_yes in books and books[new_yes].get("best_ask"):
        books[new_yes] = make_book(min(float(books[new_yes]["best_ask"])+0.06*step, 0.90), depth=max(0.5, 4.0-step*1.5))

def run_fire_window(fire, books, budget_usdc, now, scramble=True):
    remaining = size_legs(fire, budget_usdc)
    fills = {name: {"shares": Decimal("0"), "cost": Decimal("0")} for name in remaining}
    log = []
    for step, elapsed in [(0,0),(1,1600),(2,4100),(3,8100)]:
        if scramble and step>0:
            apply_scramble(books, fire.get("broken_no_token"), fire.get("new_yes_token"), step)
        for intent in plan_fire_cycle(fire, books, remaining, now+timedelta(milliseconds=elapsed), elapsed):
            log.append(intent)
            if intent.get("status") != "send_fak":
                continue
            match = paper_match_fak(books.get(intent["token_id"]) or {}, Decimal(intent["limit_price"]), Decimal(intent["shares"]))
            fills[intent["leg"]]["shares"] += match["filled_shares"]
            fills[intent["leg"]]["cost"] += match["cost"]
            remaining[intent["leg"]] = match["unfilled"]
            intent["fill"] = {"filled": str(match["filled_shares"]), "avg": str(match["avg_price"]) if match["avg_price"] is not None else None, "unfilled": str(match["unfilled"])}
    return fills, remaining, log

def scenario_one_bucket_fill():
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    books={"NO-31": make_book(0.42, depth=10), "YES-32": make_book(0.28, depth=8)}
    actions=[]
    for temp, offset in ((30.2,0),(30.8,30),(31.4,60),(32.1,90)):
        t=now+timedelta(seconds=offset)
        actions.extend(maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, temp, t, t, books))
    fire=next(a for a in actions if a.get("action_type")=="re_fire")
    fills, leftover, log = run_fire_window(fire, books, Decimal("20"), now+timedelta(seconds=90))
    return {"name":"one_bucket_fill","actions":[a["action_type"] for a in actions],"fire_jump":fire["jump"],"fills":{k:{kk:str(vv) for kk,vv in v.items()} for k,v in fills.items()},"leftover":{k:str(v) for k,v in leftover.items()},"send_faks":sum(1 for x in log if x.get("status")=="send_fak"),"ok":fills["buy_no_broken"]["shares"]>0}

def scenario_two_bucket_yes_skipped():
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 33.2, now, now, {})
    fire=next((a for a in actions if a.get("action_type")=="re_fire"), None)
    legs=[x["leg"] for x in (fire or {}).get("legs", [])]
    return {"name":"two_bucket_yes_skipped","types":[a["action_type"] for a in actions],"legs":legs,"ok":fire is not None and "buy_yes_new" not in legs and "buy_no_broken" in legs}

def scenario_stale_obs_no_fire():
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now-timedelta(minutes=10), now, {})
    return {"name":"stale_obs_no_fire","types":[a["action_type"] for a in actions],"ok":all(a.get("action_type")!="re_fire" for a in actions)}

def scenario_morning_skip():
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,3,0,tzinfo=timezone.utc)
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now, now, {})
    return {"name":"morning_skip","types":[a["action_type"] for a in actions],"ok":all(a.get("action_type")!="re_fire" for a in actions)}

def scenario_cap_abort():
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    books={"NO-31": make_book(0.80, depth=10), "YES-32": make_book(0.70, depth=8)}
    actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now, now, books)
    fire=next(a for a in actions if a["action_type"]=="re_fire")
    fills, leftover, log = run_fire_window(fire, books, Decimal("20"), now, scramble=False)
    aborted=[x for x in log if x.get("status")=="abort_above_cap"]
    return {"name":"cap_abort_no_chase","aborted":len(aborted),"filled_no":str(fills.get("buy_no_broken",{}).get("shares",0)),"ok":fills.get("buy_no_broken",{}).get("shares",0)==0 and len(aborted)>=1}

def scenario_no_double_fire():
    state={}; city=make_city(); buckets=make_buckets()
    now=datetime(2026,9,1,8,0,tzinfo=timezone.utc)
    a1=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.1, now, now, {})
    a2=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, 32.4, now, now, {})
    fires=[a for a in a1+a2 if a.get("action_type")=="re_fire"]
    return {"name":"no_double_fire","fires":len(fires),"second":[a.get("reason") for a in a2],"ok":len(fires)==1 and a2[0].get("reason")=="already_fired"}

def run_scenarios():
    results=[]; failed=0
    for fn in (scenario_one_bucket_fill, scenario_two_bucket_yes_skipped, scenario_stale_obs_no_fire, scenario_morning_skip, scenario_cap_abort, scenario_no_double_fire):
        r=fn(); results.append(r)
        if not r.get("ok"): failed += 1
    return results, failed

def live_loop(seconds, budget, tick):
    state={}; city=make_city(); buckets=make_buckets()
    base_local=datetime(2026,9,1,16,0,tzinfo=ZoneInfo(TZ))
    books={"NO-31": make_book(0.40, depth=12), "YES-32": make_book(0.30, depth=9)}
    journal=[]; fired_event=None; fill_result=None; temps=[]
    t0=time.time(); end=t0+seconds; step_i=0
    while time.time()<end:
        elapsed=time.time()-t0; frac=elapsed/max(seconds,1)
        temp = 30.4 if frac<0.25 else 30.9 if frac<0.45 else 31.2 if frac<0.55 else 32.15
        synth=(base_local+timedelta(seconds=elapsed)).astimezone(timezone.utc)
        actions=maybe_arm_or_fire(state, city, "2026-09-01", "high", buckets, 31.0, temp, synth, synth, books)
        for a in actions:
            journal.append({"t":round(elapsed,2),"temp":temp,"action_type":a.get("action_type"),"reason":a.get("reason")})
            if a.get("action_type")=="re_fire" and fired_event is None:
                fired_event=a
                fill_result=run_fire_window(a, deepcopy(books), budget, synth, scramble=True)
        step_i += 1; temps.append(temp); time.sleep(tick)
    fills, leftover, log = fill_result if fill_result else ({}, {}, [])
    return {"seconds":seconds,"steps":step_i,"last_temp":temps[-1] if temps else None,"journal_types":[j.get("action_type") for j in journal],"fired":fired_event is not None,"jump":(fired_event or {}).get("jump"),"fills":{k:{kk:str(vv) for kk,vv in v.items()} for k,v in fills.items()} if fills else {},"leftover":{k:str(v) for k,v in leftover.items()} if leftover else {},"fak_intents":[x.get("status") for x in log],"positions":ensure_re_state(state)["fired"]}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--seconds", type=int, default=30)
    p.add_argument("--budget", type=float, default=20.0)
    p.add_argument("--tick", type=float, default=1.0)
    p.add_argument("--scenarios-only", action="store_true")
    args=p.parse_args()
    scenarios, failed = run_scenarios()
    out={"scenarios":scenarios,"scenario_failures":failed}
    if not args.scenarios_only:
        out["live_loop"]=live_loop(args.seconds, Decimal(str(args.budget)), args.tick)
    print(json.dumps(out, indent=2, default=str))
    if failed: raise SystemExit(1)

if __name__=="__main__":
    main()
