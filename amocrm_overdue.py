"""Python 3.10+; install requests; env: AMO_SUBDOMAIN, AMO_ACCESS_TOKEN."""
import json, os, re, time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def report():
    host = os.environ["AMO_SUBDOMAIN"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", host):
        raise ValueError("AMO_SUBDOMAIN must contain only the account subdomain")
    base = f"https://{host}.amocrm.ru/api/v4/"
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    with requests.Session() as client:
        client.headers["Authorization"] = "Bearer " + os.environ["AMO_ACCESS_TOKEN"]
        client.mount("https://", HTTPAdapter(max_retries=retry))
        def pages(kind, filters=None):
            for page in range(1, 10001):
                params = {"page": page, "limit": 250, "order[id]": "asc"}
                params.update(filters or {})
                time.sleep(0.2)
                r = client.get(base + kind, params=params, timeout=(5, 30),
                               allow_redirects=False)
                if r.status_code == 204: return
                r.raise_for_status()
                if r.status_code != 200: raise RuntimeError("Unexpected HTTP status")
                data = r.json()
                rows = data.get("_embedded", {}).get(kind, [])
                if not rows: return
                yield from rows
                if not data.get("_links", {}).get("next"): return
            raise RuntimeError("Page limit reached; report is incomplete")
        leads = {x["id"]: x for x in pages("leads")
                 if x["status_id"] not in (142, 143) and not x.get("is_deleted")}
        pending, overdue, now = set(), {}, int(time.time())
        filters = {"filter[entity_type]": "leads", "filter[is_completed]": 0}
        for t in pages("tasks", filters) if leads else []:
            lid = t["entity_id"]
            if lid not in leads or t["is_completed"] or t["entity_type"] != "leads":
                continue
            pending.add(lid)
            if t["complete_till"] < now:
                overdue.setdefault(lid, set()).add(t["id"])
        return [{"lead_id": lid, "name": lead["name"],
                 "responsible_user_id": lead["responsible_user_id"],
                 "reason": "overdue" if lid in overdue else "no_open_tasks",
                 "overdue_task_ids": sorted(overdue.get(lid, []))}
                for lid, lead in leads.items() if lid not in pending or lid in overdue]

if __name__ == "__main__":
    print(json.dumps(report(), ensure_ascii=False, indent=2))
