"""Payment and bank-credit clearing pipeline (venue-clearing).

A fourth, fully independent pipeline next to venue_occupancy, venue_energy and
venue_recall: it imports payments (with aliases) and bank credits (with remark
references), derives candidate edges only when venue/currency/amount all match
and an alias intersects the references, then classifies every vertex over the
*entire* maximum-matching solution set of the current candidate graph and
manual pin/unpin decisions.

Nothing here imports the other packages, no legacy table is modified, and no
clearing decision is derived from occupancy, energy or ingredient data.
"""
