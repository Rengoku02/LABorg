"""Signals the Quartzy mirror sends to the rest of LabStock.

``requests_synced`` is how the duplicate guard hears about new requests without polling the
mirror (docs/design/mvp-a.md §7). It is sent on commit, so receivers see the saved rows, and
only by the 24/7 runs (head probe, rolling sweep, hot set, by-id checks; also when
``manage.py quartzy_sync`` runs them) and by catalog re-resolution (``tasks.resolve_type``
chunks, ``manage.py quartzy_resolve``). The history backfill doesn't send it: history is the
replay's job, and arming happens after the backfill anyway.

It is sent once per run, when the run's SyncRun is saved (``sync.recorded_run``), with
``SyncRun`` as the sender. A run that failed or aborted part-way still sends what it saved; a
run whose transaction rolls back (``--dry-run``) sends nothing.

Keyword arguments:

- ``qlab_id``: the QuartzyLab's pk.
- ``request_ids``: pks of OrderRequests this run created, plus those whose catalog resolution
  changed (status, supplier product or product). Ascending; never empty. Live ones only
  (``OrderRequest.objects.live()``): never a request deleted in Quartzy, nor one of a Quartzy
  lab that is not mirrored.
- ``via``: the SyncRun kind that sent it (``head``, ``sweep``, ``hot``, ``resolve``).

Receivers must be quick and must not raise: enqueue a Django-Q2 task with the ids. One that
raises anyway is logged, and neither fails the run nor keeps the other receivers from it.
"""

from django.dispatch import Signal

requests_synced = Signal()
