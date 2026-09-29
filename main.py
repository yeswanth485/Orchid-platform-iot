import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field


# ============================================================
# CONFIGURATION
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL")

INDIA_TZ = ZoneInfo("Asia/Kolkata")


def get_default_tariff() -> float:
    """
    Read the default electricity tariff from Render environment
    variables.

    Example:
    TARIFF_PER_KWH=8.00
    """

    raw_value = os.getenv("TARIFF_PER_KWH", "8.00")

    try:
        value = float(raw_value)

        if value <= 0:
            return 8.00

        return value

    except ValueError:
        return 8.00


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is not configured."
        )

    return psycopg.connect(DATABASE_URL)


# ============================================================
# DATABASE INITIALIZATION
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
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title="Orchid Smart Energy Monitoring API",
    version="3.0.0",
    lifespan=lifespan
)


# ============================================================
# TELEMETRY MODEL
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
        "version": "3.0.0"
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
# LATEST TELEMETRY
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


# ============================================================
# HOURLY ENERGY
# ============================================================

@app.get("/api/v1/energy/hourly/{device_id}")
def hourly_energy(
    device_id: str,
    report_date: Optional[date] = Query(
        default=None,
        description="Date in YYYY-MM-DD format"
    ),
    tariff_per_kwh: float = Query(
        default=8.00,
        gt=0,
        description="Electricity tariff per kWh"
    )
):

    if report_date is None:

        report_date = datetime.now(
            INDIA_TZ
        ).date()

    # India local midnight
    start_local = datetime.combine(
        report_date,
        time.min,
        tzinfo=INDIA_TZ
    )

    end_local = (
        start_local +
        timedelta(days=1)
    )

    # Convert to UTC for database filtering
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    try:

        with get_connection() as conn:

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT
                        date_trunc(
                            'hour',
                            recorded_at AT TIME ZONE 'Asia/Kolkata'
                        ) AS hour_local,

                        MIN(energy_kwh) AS first_energy_kwh,

                        MAX(energy_kwh) AS last_energy_kwh,

                        AVG(power) AS average_power_w,

                        MAX(power) AS peak_power_w,

                        AVG(current) AS average_current_a,

                        COUNT(*) AS readings

                    FROM telemetry

                    WHERE device_id = %s

                    AND recorded_at >= %s

                    AND recorded_at < %s

                    GROUP BY hour_local

                    ORDER BY hour_local;
                    """,
                    (
                        device_id,
                        start_utc,
                        end_utc
                    )
                )

                rows = cursor.fetchall()

        hourly_data = []

        total_energy = 0.0

        for row in rows:

            hour_local = row[0]

            first_energy = float(
                row[1]
            )

            last_energy = float(
                row[2]
            )

            # Cumulative PZEM energy
            # Difference = energy used
            if last_energy >= first_energy:

                consumed = (
                    last_energy -
                    first_energy
                )

            else:

                # Counter reset protection
                consumed = 0.0

            cost = (
                consumed *
                tariff_per_kwh
            )

            total_energy += consumed

            hour_start = (
                hour_local
                .replace(tzinfo=INDIA_TZ)
            )

            hourly_data.append(
                {
                    "hour_start": hour_start.isoformat(),

                    "energy_consumed_kwh":
                        round(consumed, 6),

                    "estimated_cost":
                        round(cost, 2),

                    "average_power_w":
                        round(float(row[3]), 2),

                    "peak_power_w":
                        round(float(row[4]), 2),

                    "average_current_a":
                        round(float(row[5]), 3),

                    "readings":
                        int(row[6])
                }
            )

        return {

            "device_id":
                device_id,

            "report_date":
                report_date.isoformat(),

            "tariff_per_kwh":
                tariff_per_kwh,

            "total_energy_kwh":
                round(total_energy, 6),

            "total_estimated_cost":
                round(
                    total_energy *
                    tariff_per_kwh,
                    2
                ),

            "hours_with_data":
                len(hourly_data),

            "data":
                hourly_data
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=f"Failed to calculate hourly energy: {str(e)}"
        )


# ============================================================
# DAILY ENERGY
# ============================================================

@app.get("/api/v1/energy/daily/{device_id}")
def daily_energy(
    device_id: str,
    days: int = Query(
        default=7,
        ge=1,
        le=90,
        description="Number of days including today"
    ),
    tariff_per_kwh: float = Query(
        default=8.00,
        gt=0
    )
):

    today_local = datetime.now(
        INDIA_TZ
    ).date()

    start_date = (
        today_local -
        timedelta(days=days - 1)
    )

    start_local = datetime.combine(
        start_date,
        time.min,
        tzinfo=INDIA_TZ
    )

    end_local = (
        datetime.combine(
            today_local,
            time.min,
            tzinfo=INDIA_TZ
        )
        +
        timedelta(days=1)
    )

    start_utc = start_local.astimezone(
        timezone.utc
    )

    end_utc = end_local.astimezone(
        timezone.utc
    )

    try:

        with get_connection() as conn:

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT

                        (
                            recorded_at
                            AT TIME ZONE 'Asia/Kolkata'
                        )::date AS local_date,

                        MIN(energy_kwh)
                            AS first_energy_kwh,

                        MAX(energy_kwh)
                            AS last_energy_kwh,

                        AVG(power)
                            AS average_power_w,

                        MAX(power)
                            AS peak_power_w,

                        COUNT(*)
                            AS readings

                    FROM telemetry

                    WHERE device_id = %s

                    AND recorded_at >= %s

                    AND recorded_at < %s

                    GROUP BY local_date

                    ORDER BY local_date;
                    """,
                    (
                        device_id,
                        start_utc,
                        end_utc
                    )
                )

                rows = cursor.fetchall()

        daily_data = []

        total_energy = 0.0

        for row in rows:

            first_energy = float(
                row[1]
            )

            last_energy = float(
                row[2]
            )

            if last_energy >= first_energy:

                consumed = (
                    last_energy -
                    first_energy
                )

            else:

                consumed = 0.0

            cost = (
                consumed *
                tariff_per_kwh
            )

            total_energy += consumed

            daily_data.append(
                {
                    "date":
                        row[0].isoformat(),

                    "energy_consumed_kwh":
                        round(
                            consumed,
                            6
                        ),

                    "estimated_cost":
                        round(
                            cost,
                            2
                        ),

                    "average_power_w":
                        round(
                            float(row[3]),
                            2
                        ),

                    "peak_power_w":
                        round(
                            float(row[4]),
                            2
                        ),

                    "readings":
                        int(row[5])
                }
            )

        return {

            "device_id":
                device_id,

            "start_date":
                start_date.isoformat(),

            "end_date":
                today_local.isoformat(),

            "days_requested":
                days,

            "tariff_per_kwh":
                tariff_per_kwh,

            "total_energy_kwh":
                round(
                    total_energy,
                    6
                ),

            "total_estimated_cost":
                round(
                    total_energy *
                    tariff_per_kwh,
                    2
                ),

            "data":
                daily_data
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=f"Failed to calculate daily energy: {str(e)}"
        )


# ============================================================
# ENERGY SUMMARY
# ============================================================

@app.get("/api/v1/energy/summary/{device_id}")
def energy_summary(
    device_id: str,
    report_date: Optional[date] = Query(
        default=None
    ),
    tariff_per_kwh: float = Query(
        default=8.00,
        gt=0
    )
):

    if report_date is None:

        report_date = datetime.now(
            INDIA_TZ
        ).date()

    start_local = datetime.combine(
        report_date,
        time.min,
        tzinfo=INDIA_TZ
    )

    end_local = (
        start_local +
        timedelta(days=1)
    )

    start_utc = (
        start_local.astimezone(
            timezone.utc
        )
    )

    end_utc = (
        end_local.astimezone(
            timezone.utc
        )
    )

    try:

        with get_connection() as conn:

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT

                        COUNT(*) AS readings,

                        MIN(energy_kwh)
                            AS first_energy_kwh,

                        MAX(energy_kwh)
                            AS last_energy_kwh,

                        AVG(power)
                            AS average_power_w,

                        MAX(power)
                            AS peak_power_w,

                        AVG(current)
                            AS average_current_a,

                        MAX(current)
                            AS peak_current_a,

                        MIN(recorded_at)
                            AS first_recorded_at,

                        MAX(recorded_at)
                            AS last_recorded_at

                    FROM telemetry

                    WHERE device_id = %s

                    AND recorded_at >= %s

                    AND recorded_at < %s;
                    """,
                    (
                        device_id,
                        start_utc,
                        end_utc
                    )
                )

                row = cursor.fetchone()

        readings = int(row[0])

        if readings == 0:

            return {

                "device_id":
                    device_id,

                "report_date":
                    report_date.isoformat(),

                "message":
                    "No telemetry data found for this date.",

                "readings":
                    0
            }

        first_energy = float(
            row[1]
        )

        last_energy = float(
            row[2]
        )

        if last_energy >= first_energy:

            consumed = (
                last_energy -
                first_energy
            )

        else:

            consumed = 0.0

        estimated_cost = (
            consumed *
            tariff_per_kwh
        )

        return {

            "device_id":
                device_id,

            "report_date":
                report_date.isoformat(),

            "readings":
                readings,

            "energy_start_kwh":
                round(
                    first_energy,
                    6
                ),

            "energy_end_kwh":
                round(
                    last_energy,
                    6
                ),

            "energy_consumed_kwh":
                round(
                    consumed,
                    6
                ),

            "average_power_w":
                round(
                    float(row[3]),
                    2
                ),

            "peak_power_w":
                round(
                    float(row[4]),
                    2
                ),

            "average_current_a":
                round(
                    float(row[5]),
                    3
                ),

            "peak_current_a":
                round(
                    float(row[6]),
                    3
                ),

            "estimated_cost":
                round(
                    estimated_cost,
                    2
                ),

            "first_recorded_at":
                row[7],

            "last_recorded_at":
                row[8]
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=f"Failed to calculate energy summary: {str(e)}"
        )