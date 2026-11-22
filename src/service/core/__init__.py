"""LaneLead platform interfaces (the 'D' layer of docs/LANELEAD.md).

Each module is a thin, typed interface over the existing ledger so the rest of the
system (signals, routes, chat) builds against one contract:

  evidence.py  save_fact() / get_fact()          every fact gets an evidence ID
  memory.py    AccountMemory.last_contact() / record_action()
  tasks.py     run_task(type, params)            job ID; resumes after a kill
  outbox.py    queue_action() / approve()        sends once (step 10 sends)
  results.py   kept / held / cannot_verify       the shared result rule
"""
