"""Storage: the only code that reads and writes the data folder (coding rule 4).

    local.py    JobStore: one folder per uploaded document under data/jobs/<job_id>/

Feature code never builds file paths itself; it asks the store. That keeps the path-safety
checks (no "../" tricks) in one place.
"""
