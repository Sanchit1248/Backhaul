"""Chat layer (the 'C' layer of docs/LANELEAD.md).

  parser.py   question -> one task (rules first, Claude tool calling as fallback); the
              parsed task is shown to the person before anything runs
  memory.py   conversation memory: turns, and follow-ups like "the second one"
  answer.py   answer lines that may state a fact only with an evidence ID
"""
