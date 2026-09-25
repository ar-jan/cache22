"""Run with the development environment: python utils/benchmark_inventory.py.

Synthetic smoke measurements, not a latency guarantee. No user data is accessed.
"""

from functools import partial
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from starlette.testclient import TestClient

from cache22.index import Index
from cache22.inventory_service import (
    InventoryQuery,
    InventoryRequest,
    export_inventory,
    inventory_page,
    selected_ids,
)
from cache22.manager.app import create_app


def main() -> None:
    with TemporaryDirectory(prefix="cache22-inventory-benchmark-") as directory:
        index = Index.initialize(Path(directory) / "index.db")
        with index.transaction() as db:
            db.execute("""WITH RECURSIVE seq(n) AS (
              SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<10000)
              INSERT INTO repositories(id,repo_key,display_path,host,source_url,source_path,
                archive_root,created_at,updated_at,local_state,local_ref_digest,remote_ref_digest)
              SELECT n,printf('host%d/team/repo%05d',n%10,n),printf('host%d/Team/Repo%05d',n%10,n),
                printf('host%d',n%10),printf('https://host%d/team/repo%05d',n%10,n),
                printf('team/repo%05d',n),'/unavailable/archive',1,1,
                CASE n%3 WHEN 0 THEN 'ready' WHEN 1 THEN 'absent' ELSE 'missing' END,
                'a',CASE n%2 WHEN 0 THEN 'a' ELSE 'b' END FROM seq""")
            db.execute("""INSERT INTO jobs(id,repository_id,kind,origin,state,due_at,created_at,finished_at)
              SELECT id,id,'fetch','manual','succeeded',1,1,2 FROM repositories""")
            db.execute("""INSERT INTO job_attempts(job_id,kind,started_at,finished_at,outcome)
              SELECT id,'fetch',1,2,'succeeded' FROM jobs""")
            db.execute("""INSERT INTO jobs(repository_id,kind,origin,state,due_at,created_at)
              SELECT id,'check','manual','pending',3,3 FROM repositories WHERE id%5=0""")
            db.execute("""INSERT INTO schedules(repository_id,enabled,interval_seconds,next_due_at)
              SELECT id,1,3600,3600 FROM repositories WHERE id%2=0""")
            db.execute("""INSERT INTO jobs(repository_id,kind,origin,state,due_at,created_at,finished_at)
              SELECT id,'check','manual','failed',3,3,4 FROM repositories WHERE id%7=0""")
            db.execute("""INSERT INTO job_attempts(job_id,kind,started_at,finished_at,outcome,error_category,error)
              SELECT id,'check',3,4,'failed','transport','Unavailable' FROM jobs WHERE state='failed'""")
        app = create_app(index.path)
        with TestClient(app, base_url="http://localhost") as client:
            cases = {
                "First page, count and three facets": partial(inventory_page, index),
                "Deep page, count and three facets": partial(
                    inventory_page, index, InventoryRequest(offset=9900)
                ),
                "Filtered page and facets": partial(
                    inventory_page,
                    index,
                    InventoryRequest(InventoryQuery(host=("host0",), queued=True, scheduled=True)),
                ),
                "Derived date sort and facets": partial(
                    inventory_page,
                    index,
                    InventoryRequest(InventoryQuery(sort="last_fetched_at", descending=True)),
                ),
                "Diagnostic filter and facets": partial(
                    inventory_page, index, InventoryRequest(InventoryQuery(has_error=True))
                ),
                "Capture 10,000 IDs": partial(selected_ids, index, InventoryQuery()),
                "Search and capture IDs": partial(selected_ids, index, InventoryQuery(q="repo000")),
                "Export service, 10,000 rows": partial(export_inventory, index, InventoryQuery()),
                "HTTP inventory HTML": partial(client.get, "/"),
                "HTTP inventory fragment": partial(client.get, "/inventory/fragment"),
                "HTTP JSON export": partial(client.get, "/inventory.json"),
                "HTTP CSV export": partial(client.get, "/inventory.csv"),
            }
            print("| Operation | Median ms (5 runs) |")
            print("| --- | ---: |")
            for name, run in cases.items():
                timings = []
                for _ in range(5):
                    started = perf_counter()
                    result = run()
                    timings.append((perf_counter() - started) * 1000)
                    if hasattr(result, "status_code"):
                        assert result.status_code == 200
                print(f"| {name} | {median(timings):.1f} |", flush=True)


if __name__ == "__main__":
    main()
