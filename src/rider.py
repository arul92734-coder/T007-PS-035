"""Rider model and registration logic."""

class Rider:
    def __init__(
        self,
        rider_id,
        name,
        vehicle_type,
        shift_start,
        shift_end,
        capacity_parcels,
        capacity_kg,
        home_hub,
    ):
        self.rider_id = rider_id
        self.name = name
        self.vehicle_type = vehicle_type
        self.shift_start = shift_start
        self.shift_end = shift_end
        self.capacity_parcels = capacity_parcels
        self.capacity_kg = capacity_kg
        self.home_hub = home_hub
