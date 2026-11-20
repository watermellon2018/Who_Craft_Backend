"""Separate immutable approved routes from the selected execution attempt."""


def execution_route(job) -> dict:
    snapshot = job.provider_snapshot
    routes = snapshot.get("candidateRoutes")
    if routes:
        return {**snapshot, **routes[job.execution_candidate_index]}
    return snapshot
