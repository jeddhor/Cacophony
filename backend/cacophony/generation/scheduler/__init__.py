"""Job scheduling (design document sections 29, 30 and 84).

Arrives with the job-system phase: durable jobs, per-provider concurrency
limits, backpressure and checkpointed resume. Section 39 is explicit that a
lightweight internal asynchronous job manager should be sufficient initially,
and that Redis and Celery should be avoided until distributed execution is
actually required.
"""
