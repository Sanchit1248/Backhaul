"""Signals layer (the 'A' layer of docs/LANELEAD.md) as task functions.

  targets.py  find_targets(routes, window, count): the ten-step pipeline per lane with
              free capacity, returned as targets with evidence IDs
  churn.py    check_churn(clients, window): order-volume trend per client from the
              route adapter, risk level from config churn.*
"""
