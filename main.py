from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel, Field


app = FastAPI(
    title="Orchid Smart Energy Monitoring API",
    version="1.0.0"
)


# ============================================================
# TELEMETRY DATA MODEL
# ============================================================

class EnergyReading(BaseModel):
    device_id: str = Field(..., min_length=1)
    branch_id: str = Field(..., min_length=1)
    asset_id: str = Field(..., min_length=1)

    voltage: float
    current: float
    power: float
    energy_kwh: float
    frequency: float
    power_factor: float


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():
    return {
        "status": "online",
        "service": "Orchid Smart Energy Monitoring API",
        "version": "1.0.0"
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health():
    return {
        "status": "healthy"
    }


# ============================================================
# RECEIVE ESP32 TELEMETRY
# ============================================================

@app.post("/api/v1/telemetry")
def receive_telemetry(reading: EnergyReading):

    received_at = datetime.now(timezone.utc)

    print("=" * 60)
    print("ENERGY TELEMETRY RECEIVED")
    print("=" * 60)

    print(f"Device ID    : {reading.device_id}")
    print(f"Branch ID    : {reading.branch_id}")
    print(f"Asset ID     : {reading.asset_id}")

    print(f"Voltage      : {reading.voltage} V")
    print(f"Current      : {reading.current} A")
    print(f"Power        : {reading.power} W")
    print(f"Energy       : {reading.energy_kwh} kWh")
    print(f"Frequency    : {reading.frequency} Hz")
    print(f"Power Factor : {reading.power_factor}")

    print(f"Received At  : {received_at.isoformat()}")

    print("=" * 60)

    return {
        "status": "received",
        "device_id": reading.device_id,
        "received_at": received_at.isoformat()
    }