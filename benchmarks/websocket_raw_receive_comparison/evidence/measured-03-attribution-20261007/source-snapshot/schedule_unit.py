import sys
from collections import Counter
sys.path.insert(0, "/home/baidu/scrapanium-experiments/wss-raw-recv-diagnostic-20261007")
import runner
import perf_campaign
seed=20261007
schedule=runner.balanced_schedule(seed)
assert len(schedule)==12
assert all(len(row)==12 and sorted(row)==list(range(12)) for row in schedule)
positions=[[0]*12 for _ in range(12)]
for row in schedule:
    for position,cell in enumerate(row):
        positions[cell][position]+=1
assert all(value==1 for row in positions for value in row)
full=runner.plan_schedule(False,seed,"unit")
smoke=runner.plan_schedule(True,seed,"smoke-unit")
assert len(full)==168 and sum(row["kind"]=="attribution_sample" for row in full)==144
assert sum(row["kind"]=="negative_correctness_control" for row in full)==24
assert len(smoke)==36 and sum(row["kind"]=="correctness_smoke" for row in smoke)==12
assert sum(row["kind"]=="negative_correctness_control" for row in smoke)==24
profile=perf_campaign.schedule_for_seed(seed)
assert len(profile)==12 and all(len(row)==6 for row in profile)
assert profile==perf_campaign.schedule_for_seed(seed)
profile_positions=[[0]*6 for _ in range(6)]
for row in profile:
    for position,cell in enumerate(row):
        profile_positions[cell][position]+=1
assert all(value==2 for row in profile_positions for value in row)
print("unit-ok positives=144 negatives=24 smoke-positives=12 smoke-negatives=24 positions=12x12 profile-captures=72 profile-positions=6x12x2")

