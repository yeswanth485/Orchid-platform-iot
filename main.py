import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


# ============================================================
# DATABASE
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL")


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set")

    return psycopg.connect(DATABASE_URL)


# ============================================================
# CREATE DATABASE TABLE
# ============================================================

def initialize_database():

    with get_connection() as conn:

        with conn.cursor() as cursor:

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry (
                    id BIGSERIAL PRIMARY KEY,

                    device_id VARCHAR(100) NOT NULL,
                    branch_id VARCHAR(100) NOT NULL,
                    asset_id VARCHAR(100) NOT NULL,

                    voltage DOUBLE PRECISION NOT NULL,
                    current DOUBLE PRECISION NOT NULL,
                    power DOUBLE PRECISION NOT NULL,

                    energy_kwh DOUBLE PRECISION NOT NULL,

                    frequency DOUBLE PRECISION NOT NULL,
                    power_factor DOUBLE PRECISION NOT NULL,

                    recorded_at TIMESTAMPTZ NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_telemetry_device_time
                ON telemetry (device_id, recorded_at DESC);
                """
            )

        conn.commit()


# ============================================================
# APPLICATION LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    initialize_database()

    yield


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Orchid Smart Energy Monitoring API",
    version="2.0.0",
    lifespan=lifespan
)


# ============================================================
# DATA MODEL
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
        "version": "2.0.0"
    }


# ============================================================
# BASIC HEALTH
# ============================================================

@app.get("/health")
def health():

    return {
        "status": "healthy"
    }


# ============================================================
# DATABASE HEALTH
# ============================================================

@app.get("/health/db")
def database_health():

    try:

        with get_connection() as conn:

            with conn.cursor() as cursor:

                cursor.execute("SELECT 1")

                result = cursor.fetchone()

        return {
            "status": "healthy",
            "database": "connected",
            "test": result[0]
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=f"Database connection failed: {str(e)}"
        )


# ============================================================
# RECEIVE TELEMETRY
# ============================================================

@app.post("/api/v1/telemetry")
def receive_telemetry(reading: EnergyReading):

    recorded_at = datetime.now(timezone.utc)

    try:

        with get_connection() as conn:

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    INSERT INTO telemetry (
                        device_id,
                        branch_id,
                        asset_id,
                        voltage,
                        current,
                        power,
                        energy_kwh,
                        frequency,
                        power_factor,
                        recorded_at
                    )

                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s
                    )

                    RETURNING id;
                    """,

                    (
                        reading.device_id,
                        reading.branch_id,
                        reading.asset_id,
                        reading.voltage,
                        reading.current,
                        reading.power,
                        reading.energy_kwh,
                        reading.frequency,
                        reading.power_factor,
                        recorded_at
                    )
                )

                row = cursor.fetchone()

            conn.commit()

        telemetry_id = row[0]

        print("=" * 60)
        print("ENERGY TELEMETRY STORED")
        print("=" * 60)

        print(f"Telemetry ID : {telemetry_id}")

        print(f"Device ID    : {reading.device_id}")
        print(f"Branch ID    : {reading.branch_id}")
        print(f"Asset ID     : {reading.asset_id}")

        print(f"Voltage      : {reading.voltage} V")
        print(f"Current      : {reading.current} A")
        print(f"Power        : {reading.power} W")
        print(f"Energy       : {reading.energy_kwh} kWh")
        print(f"Frequency    : {reading.frequency} Hz")
        print(f"Power Factor : {reading.power_factor}")

        print(f"Recorded At  : {recorded_at.isoformat()}")

        print("=" * 60)

        return {
            "status": "received",
            "stored": True,
            "telemetry_id": telemetry_id,
            "device_id": reading.device_id,
            "recorded_at": recorded_at.isoformat()
        }

    except Exception as e:

        print("DATABASE INSERT ERROR")
        print(str(e))

        raise HTTPException(
            status_code=500,
            detail="Failed to store telemetry"
        )


# ============================================================
# LATEST READING
# ============================================================

@app.get("/api/v1/telemetry/latest/{device_id}")
def latest_telemetry(device_id: str):

    try:

        with get_connection() as conn:

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT
                        id,
                        device_id,
                        branch_id,
                        asset_id,
                        voltage,
                        current,
                        power,
                        energy_kwh,
                        frequency,
                        power_factor,
                        recorded_at,
                        received_at
                    FROM telemetry

                    WHERE device_id = %s

                    ORDER BY recorded_at DESC

                    LIMIT 1;
                    """,

                    (device_id,)
                )

                row = cursor.fetchone()

        if row is None:

            raise HTTPException(
                status_code=404,
                detail="No telemetry found for this device"
            )

        return {
            "id": row[0],
            "device_id": row[1],
            "branch_id": row[2],
            "asset_id": row[3],
            "voltage": row[4],
            "current": row[5],
            "power": row[6],
            "energy_kwh": row[7],
            "frequency": row[8],
            "power_factor": row[9],
            "recorded_at": row[10],
            "received_at": row[11]
        }

    except HTTPException:

        raise

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=f"Failed to read telemetry: {str(e)}"
        )