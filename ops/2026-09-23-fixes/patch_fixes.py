"""Apply the 2026-09-23 bug fixes to the DEPLOYED CCR twin (with .bak backups).

Fixes:
  1. train_gbt: cap LightGBM threads (retrain took ~9 h from OpenMP oversubscription)
                + expose the t+5 test fold so the guard can score old vs new on it.
  2. retrain:   same-window guard (old model re-scored on the new test rows) and
                skip re-runs < 5 days apart (PM2 resurrect launches it on every boot).
  3. monitor:   accuracy vs the 3-min-centred 1-min-mean truth the model is trained on.
Every anchor must match exactly once; otherwise the file is left untouched.
"""
import shutil
import time
from pathlib import Path

ROOT = Path("/home/antpc/CCR-Project")
TS = time.strftime("%Y%m%d-%H%M%S")


def patch(rel, edits):
    p = ROOT / rel
    src = p.read_text()
    for old, new in edits:
        assert src.count(old) == 1, f"{rel}: anchor not unique/found: {old[:70]!r}"
        src = src.replace(old, new)
    shutil.copy2(p, p.with_name(p.name + f".bak-{TS}"))
    p.write_text(src)
    print("patched", rel, "->", p.name + f".bak-{TS}")


# 1) train_gbt: thread cap + expose the t+5 test fold ---------------------------
patch("twin/train_gbt.py", [
    ('_NUM_ROUNDS = 2000\n',
     '_NUM_ROUNDS = 2000\n'
     '# 2026-09-23: cap threads. Without it LightGBM grabbed all 32 cores while the\n'
     '# shadow (~8) + optimizer (~5) loops were busy -> OpenMP oversubscription made\n'
     '# the weekly retrain take ~9 h (02:00 -> 10:50). Capped fits run in 1-3 min each.\n'
     '_THREADS = 20\n'),
    ('    "verbose": -1,\n}',
     '    "verbose": -1,\n    "num_threads": _THREADS,\n}'),
    ('def train(sample: int | None) -> dict:',
     'def train(sample: int | None, test_out: dict | None = None) -> dict:'),
    ('            metrics["n_train"] = int(len(x_tr))\n            metrics["n_test"] = int(len(x_te))\n',
     '            metrics["n_train"] = int(len(x_tr))\n            metrics["n_test"] = int(len(x_te))\n'
     '            if test_out is not None:            # same-window retrain guard\n'
     '                test_out.update({"x": x_te, "y_raw": yt, "cv": cv_np,\n'
     '                                 "feature_columns": list(x_te.columns)})\n'),
])

# 2) retrain: boot-resurrect skip + same-window guard ---------------------------
patch("twin/retrain.py", [
    ('GUARD_SLACK = 1.15          # new test MAE may be at most 15% above the old\n',
     'GUARD_SLACK = 1.15          # legacy rule (different windows): <=15% worse\n'
     'SAME_WINDOW_SLACK = 1.02    # 2026-09-23: old vs new scored on the SAME test rows\n'
     'MIN_GAP_DAYS = 5            # PM2 resurrect re-launches this job on every reboot\n'),
    ('def _mae() -> float | None:',
     'def _last_run() -> dt.datetime | None:\n'
     '    try:\n'
     '        last = LOG.read_text().strip().splitlines()[-1]\n'
     '        return dt.datetime.fromisoformat(json.loads(last)["ts"])\n'
     '    except Exception:\n'
     '        return None\n\n\n'
     'def _score_prev_same_window(test: dict) -> float | None:\n'
     '    """Previous model\'s served-mean MAE on the NEW model\'s test rows (or None)."""\n'
     '    try:\n'
     '        import joblib\n'
     '        import numpy as np\n'
     '        from twin import features as F\n'
     '        b = joblib.load(PREV / PKL.name)\n'
     '        if list(b["feature_columns"]) != test["feature_columns"]:\n'
     '            return None                      # features changed -> not comparable\n'
     '        hb = b["temp_multi"][F.HORIZON_MIN]\n'
     '        w = float(hb["arx"]["w"])\n'
     '        gbt = hb["mean"].predict(test["x"])\n'
     '        arx = test["x"].to_numpy(float) @ hb["arx"]["coef"] + hb["arx"]["intercept"]\n'
     '        pred = (1 - w) * gbt + w * arx + test["cv"]\n'
     '        return round(float(np.mean(np.abs(pred - test["y_raw"]))), 3)\n'
     '    except Exception as e:                   # never let the guard itself crash\n'
     '        print(f"[retrain] same-window scoring unavailable: {e}")\n'
     '        return None\n\n\n'
     'def _mae() -> float | None:'),
    ('    ap.add_argument("--no-reload", action="store_true")\n    args = ap.parse_args()\n',
     '    ap.add_argument("--no-reload", action="store_true")\n'
     '    ap.add_argument("--force", action="store_true",\n'
     '                    help="run even if the last retrain was < MIN_GAP_DAYS ago")\n'
     '    args = ap.parse_args()\n\n'
     '    last = _last_run()\n'
     '    if last and not args.force and (dt.datetime.now() - last).days < MIN_GAP_DAYS:\n'
     '        print(f"[retrain] skipped: last run {last.isoformat(timespec=\'minutes\')} "\n'
     '              f"< {MIN_GAP_DAYS} days ago (boot resurrect / manual re-run; use --force)")\n'
     '        return\n'),
    ('    from twin import train_gbt\n    train_gbt.train(None)\n    new_mae = _mae()\n    entry["new_mae"] = new_mae\n\n'
     '    promoted = True\n    if old_mae is not None and new_mae is not None and new_mae > old_mae * GUARD_SLACK:\n',
     '    from twin import train_gbt\n    test: dict = {}\n    train_gbt.train(None, test_out=test)\n    new_mae = _mae()\n    entry["new_mae"] = new_mae\n\n'
     '    # Same-window comparison: the old model re-scored on the new test rows, so\n'
     '    # the guard judges the MODEL, not the regime shift of a moving test window.\n'
     '    old_same = _score_prev_same_window(test) if test else None\n'
     '    entry["old_mae_same_window"] = old_same\n'
     '    try:\n'
     '        entry["persistence_mae"] = json.loads(MET.read_text())["baseline_persistence"]["temp"]["mae"]\n'
     '    except Exception:\n'
     '        pass\n'
     '    if old_same is not None:\n'
     '        worse = new_mae is not None and new_mae > old_same * SAME_WINDOW_SLACK\n'
     '        entry["guard"] = "same_window"\n'
     '    else:\n'
     '        worse = old_mae is not None and new_mae is not None and new_mae > old_mae * GUARD_SLACK\n'
     '        entry["guard"] = "legacy_15pct"\n\n'
     '    promoted = True\n    if worse:\n'),
    ('        print(f"[retrain] GUARD TRIPPED: new {new_mae} > {old_mae}*{GUARD_SLACK} "\n'
     '              "— previous model restored")',
     '        print(f"[retrain] GUARD TRIPPED ({entry[\'guard\']}): new {new_mae} vs old "\n'
     '              f"{old_same if old_same is not None else old_mae} — previous model restored")'),
])

# 3) monitor: accuracy vs the smoothed truth the model is trained on ------------
patch("twin/monitor.py", [
    ('import config\n', 'import config\nfrom backend.analysis import CV\n'),
    ('def _conformal(m3: pd.DataFrame) -> dict | None:',
     'def _accuracy_smooth(m: pd.DataFrame, eng) -> dict:\n'
     '    """Same predictions scored against the 3-min-centred 1-min-mean temperature.\n\n'
     '    The twin is trained on that smoothed target; the instantaneous 2-s sample\n'
     '    used by _accuracy carries ~2 degC of within-minute sensor jitter, so this\n'
     '    is the fair number. Also reports persistence + skill on the same truth.\n'
     '    """\n'
     '    if not len(m):\n'
     '        return {"n": 0}\n'
     '    try:\n'
     '        lo = (pd.to_datetime(m["ts"], format="mixed").min() - pd.Timedelta(minutes=5)).isoformat(timespec="seconds")\n'
     '        hi = (pd.to_datetime(m["horizon_ts"], format="mixed").max() + pd.Timedelta(minutes=5)).isoformat(timespec="seconds")\n'
     '        pl = pd.read_sql(text(f\'SELECT ts, "{CV}" AS v FROM process_live WHERE ts >= :lo AND ts <= :hi\'),\n'
     '                         eng, params={"lo": lo, "hi": hi})\n'
     '        pl["ts"] = pd.to_datetime(pl["ts"], format="mixed")\n'
     '        m1 = pl.set_index("ts")["v"].sort_index().resample("1min").mean()\n'
     '        truth = m1.rolling(3, center=True, min_periods=2).mean()\n'
     '        hts = pd.to_datetime(m["horizon_ts"], format="mixed").dt.floor("min")\n'
     '        t = truth.reindex(hts).to_numpy()\n'
     '        ok = ~np.isnan(t)\n'
     '        if ok.sum() < 10:\n'
     '            return {"n": int(ok.sum())}\n'
     '        err = np.abs(m["pred_mean"].to_numpy()[ok] - t[ok])\n'
     '        pers = np.abs(m["actual_now"].to_numpy()[ok] - t[ok])\n'
     '        return {"n": int(ok.sum()), "mae": _r(err.mean()), "median_ae": _r(np.median(err)),\n'
     '                "within5C": _r((err <= 5).mean(), 3), "within10C": _r((err <= 10).mean(), 3),\n'
     '                "persistence_mae": _r(pers.mean()),\n'
     '                "skill_pct": _r((1 - err.mean() / pers.mean()) * 100, 1) if pers.mean() else None,\n'
     '                "truth": "3-min centred mean of 1-min means"}\n'
     '    except Exception as e:\n'
     '        return {"n": 0, "error": str(e)[:120]}\n\n\n'
     'def _conformal(m3: pd.DataFrame) -> dict | None:'),
    ('    cut3d = (now - dt.timedelta(days=_CONF_DAYS)).isoformat(timespec="seconds")\n',
     '    cut3d = (now - dt.timedelta(days=_CONF_DAYS)).isoformat(timespec="seconds")\n'
     '    cut7d = (now - dt.timedelta(days=7)).isoformat(timespec="seconds")\n'),
    ('    m_3d = m_all[m_all["ts"] >= cut3d]\n',
     '    m_3d = m_all[m_all["ts"] >= cut3d]\n    m_7d = m_all[m_all["ts"] >= cut7d]\n'),
    ('        "acc_all": _accuracy(m_all),\n',
     '        "acc_all": _accuracy(m_all),\n'
     '        "acc_24h_smooth": _accuracy_smooth(m_24, eng),\n'
     '        "acc_7d_smooth": _accuracy_smooth(m_7d, eng),\n'),
    ('    print(f"[monitor] conformal: {entry[\'conformal\']}")',
     '    s = entry["acc_24h_smooth"]\n'
     '    print(f"[monitor] 24h vs smoothed truth: MAE={s.get(\'mae\')} persistence={s.get(\'persistence_mae\')} "\n'
     '          f"skill={s.get(\'skill_pct\')}%")\n'
     '    print(f"[monitor] conformal: {entry[\'conformal\']}")'),
])
print("ALL PATCHES APPLIED")
