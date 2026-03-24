#!/usr/bin/env python3
import os
import sys
from pathlib import Path

# Load .env file
env_path = Path('.env')
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                try:
                    key, value = line.split('=', 1)
                    os.environ[key] = value
                except ValueError:
                    # Skip lines that don't match KEY=VALUE
                    pass

# Now run the worker
sys.path.insert(0, str(Path.cwd()))
from worker import *

if __name__ == '__main__':
    setup_git()
    setup_mcporter()
    scheduler = BlockingScheduler(executors={'default': ThreadPoolExecutor(20)}, job_defaults={'misfire_grace_time': 30})
    # Import job functions after worker module is loaded
    from worker import (
        trade_evaluator_job,
        regime_classifier_job,
        portfolio_review_job,
        howl_nightly_job,
        whale_index_job,
        arena_learner_job,
        health_check_job,
        # update_skills,  # REMOVED: function no longer exists in worker.py (NameError fix)
    )
    # Schedule jobs as in worker.py
    scheduler.add_job(trade_evaluator_job, 'cron', minute='*/15', id='trade_evaluator', replace_existing=True)
    scheduler.add_job(regime_classifier_job, 'cron', hour='*', id='regime_classifier', replace_existing=True)
    scheduler.add_job(portfolio_review_job, 'cron', hour='0,6,12,18', id='portfolio_review', replace_existing=True)
    scheduler.add_job(howl_nightly_job, 'cron', hour='23', minute='55', id='howl_nightly', replace_existing=True)
    scheduler.add_job(whale_index_job, 'cron', hour='1', id='whale_index', replace_existing=True)
    scheduler.add_job(arena_learner_job, 'cron', hour='*/4', id='arena_learner', replace_existing=True)
    scheduler.add_job(health_check_job, 'cron', minute='*/5', id='health_check', replace_existing=True)
    # scheduler.add_job(update_skills, 'cron', hour='*/3', id='update_skills', replace_existing=True)  # REMOVED
    print("[scheduler] Starting APScheduler...")
    scheduler.start()