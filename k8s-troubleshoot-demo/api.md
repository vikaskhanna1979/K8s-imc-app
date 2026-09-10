4.1 POST /execute acknowledgement → HTTP 202
{ 
    "run_id": "RUN-RAN-SCN-1042-1", 
    "current_status": "ACCEPTED",
    "agent": "RAN", 
    "message": "RAN activity queued" 
}
4.2 GET /status — ACCEPTED (queued, not started)
{
    "run_id": "RUN-RAN-SCN-1042-1", 
    "mission": "Customer unable to latch onto the network",
    "agent": "RAN", 
    "current_status": "ACCEPTED",
    "current_response": "RAN accepted - waiting in queue (2.8s)",
    "progress": 0, 
    "details": { "region": "north" }, 
    "created": 1788960000.12,
    "sub_agents": { 
        "RF-Reasoner": { 
            "status": "QUEUED", 
            "response": "queued - backlog 2.8s" 
        } 
    }
}
4.3 GET /status — RUNNING (in progress)
{
    "run_id": "RUN-RAN-SCN-1042-1", 
    "mission": "Customer unable to latch onto the network",
    "agent": "RAN", 
    "current_status": "RUNNING",
    "current_response": "RAN reasoning 65% - correlating handover failures with coverage holes",
    "progress": 65, 
    "details": { "region": "north" }, 
    "created": 1788960000.12,
    "sub_agents": { 
        "RF-Reasoner": { 
            "status": "RUNNING", 
            "response": "correlating handover failures" 
        } 
    }
}
4.4 GET /status — COMPLETED (terminal, success)
{
    "run_id": "RUN-RAN-SCN-1042-1", 
    "mission": "Customer unable to latch onto the network",
    "agent": "RAN", 
    "current_status": "COMPLETED",
    "current_response": "RAN completed - radio-side root cause ranked with confidence",
    "progress": 100, 
    "details": { "region": "north" }, 
    "created": 1788960000.12,
    "sub_agents": { 
        "RF-Reasoner": { 
            "status": "COMPLETED", 
            "response": "root cause ranked" 
        } 
    }
}
4.5 GET /status — FAILED (terminal, business error — still HTTP 200)
{
      "run_id": "RUN-RAN-SCN-1042-1", 
      "mission": "Customer unable to latch onto the network",
      "agent": "RAN",
      "current_status": "FAILED",
      "current_response": "RAN failed - cell C102 unreachable after 3 retries",
      "progress": 40, 
      "details": { "region": "north" }, 
      "created": 1788960000.12,
      "sub_agents": { 
        "RF-Reasoner": { 
            "status": "FAILED", 
            "response": "cell sweep aborted" 
        } 
    }
}
4.6 GET /history — list runs kept for 30 minutes
{
    "ttl_sec": 1800,
    "count": 1,
    "runs": [
        {
            "run_id": "RUN-K8S-SCN-1042-1",
            "mission": "Why is open5gs-amf-0 CrashLoopBackOff in 5g-core?",
            "agent": "K8s",
            "current_status": "COMPLETED",
            "progress": 100,
            "session_id": "amf-crashloop",
            "created": 1788960000.12,
            "expires_at": 1788961800.12,
            "ui_url": "/?run_id=RUN-K8S-SCN-1042-1",
            "current_response": "RCA complete",
            "title": "AMF CrashLoopBackOff"
        }
    ]
}
4.7 GET /history/{run_id} — same payload as GET /status (plus expires_at)
Unknown or expired run → HTTP 404. In-flight ACCEPTED/RUNNING runs are never pruned. Terminal COMPLETED/FAILED runs drop after ttl_sec from created (default 1800). GET /status also 404s after prune.