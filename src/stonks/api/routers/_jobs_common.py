"""Helpers shared by routes that enqueue background jobs."""

from __future__ import annotations

from fastapi import Response

from stonks.app.jobs import Job

JOB_CREATED = {"status_code": 202, "response_model": Job}


def accepted(job: Job, response: Response) -> Job:
    response.headers["Location"] = f"/api/jobs/{job.id}"
    return job
