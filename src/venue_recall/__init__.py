"""Ingredient lot genealogy and recall pipeline (venue-recall).

A third, fully independent pipeline next to venue_occupancy and venue_energy:
it tracks raw-lot receives, blends and venue deliveries in one SQLite file,
propagates exact rational compositions through blends, quarantines every
descendant of a recalled raw lot, and reports affected deliveries and stock.
Nothing here imports the other two packages, and no ingredient quantity is
ever derived from bookings or meter readings.
"""
