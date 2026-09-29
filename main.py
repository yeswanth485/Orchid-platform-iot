import math
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Literal, Optional
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field


# ============================================================
# CONFIGURATION
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL")
INDIA_TZ = ZoneInfo("Asia/Kolkata")

MeasurementMode = Literal["measured_submeter", "estimated", "main_meter_allocated"]
VoltageBasis = Literal["line_to_line", "line_to_neutral"]


# ============================================================
# DATABASE
# ============================================================

def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not configured.")
    return psycopg.connect(DATABASE_URL)


def get_default_tariff() -> float:
    raw = os.getenv("TARIFF_PER_KWH", "8.00")
    try:
        value = float(raw)
        return value if value > 0 else 8.00
    except ValueError:
        return 8.00


# ============================================================
# ENGINEERING FORMULAS
# ============================================================

def active_power_from_vi_pf(
    voltage_v: float,
    current_a: float,
    power_factor: float,
    phase_count: int = 1,
    voltage_basis: str = "line_to_neutral",
) -> float:
    """Return active electrical power in kW from RMS V, A and PF.

    Single phase: P = V * I * PF / 1000.
    Balanced 3-phase with line-line voltage: P = sqrt(3)*VLL*I*PF/1000.
    Balanced 3-phase with line-neutral voltage: P = 3*VLN*I*PF/1000.

    For a real energy meter that already reports active power, prefer the
    meter-reported power as the primary measurement; this function is a
    calculation/cross-check.
    """
    pf = abs(float(power_factor))
    voltage = abs(float(voltage_v))
    current = abs(float(current_a))

    if phase_count == 1:
        return (voltage * current * pf) / 1000.0

    if phase_count == 3:
        if voltage_basis == "line_to_line":
            return (math.sqrt(3.0) * voltage * current * pf) / 1000.0
        return (3.0 * voltage * current * pf) / 1000.0

    raise ValueError("phase_count must be 1 or 3")


def rated_input_power_kw(
    tonnage_tr: Optional[float],
    rated_power_kw: Optional[float],
    eer: Optional[float],
) -> tuple[float, str]:
    """Get rated electrical input power and the formula/source used.

    Priority:
    1) Manufacturer/nameplate rated electrical input power.
    2) Cooling capacity = 3.517 kW/TR, then input power = cooling/EER.

    Tonnage alone is NOT enough to determine electrical input power.
    """
    if rated_power_kw is not None and rated_power_kw > 0:
        return float(rated_power_kw), "manufacturer_rated_input_power"

    if tonnage_tr is not None and tonnage_tr > 0 and eer is not None and eer > 0:
        cooling_capacity_kw = 3.517 * float(tonnage_tr)
        input_power_kw = cooling_capacity_kw / float(eer)
        return input_power_kw, "(3.517 * TR) / EER"

    raise ValueError(
        "Need rated_power_kw or both tonnage_tr and eer for estimation. "
        "Tonnage alone cannot determine electrical input power."
    )


def estimated_energy_kwh(
    base_power_kw: float,
    quantity: int,
    operating_factor: float,
    hours: float,
) -> float:
    if quantity < 1:
        raise ValueError("quantity must be >= 1")
    if not 0 <= operating_factor <= 1:
        raise ValueError("operating_factor must be between 0 and 1")
    if hours < 0:
        raise ValueError("hours must be >= 0")

    return float(base_power_kw) * quantity * operating_factor * hours


# ============================================================
# DATABASE INITIALIZATION / MIGRATION
# ============================================================

def initialize_database():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            # ------------------------
            # Companies
            # ------------------------
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS companies (
                    id BIGSERIAL PRIMARY KEY,
                    name VARCHAR(150) NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            # ------------------------
            # Branches
            # ------------------------
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS branches (
                    id BIGSERIAL PRIMARY KEY,
                    company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    name VARCHAR(150) NOT NULL,
                    branch_code VARCHAR(100) NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            # ------------------------
            # Assets
            # ------------------------
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    id BIGSERIAL PRIMARY KEY,
                    branch_id BIGINT NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                    asset_code VARCHAR(100) NOT NULL,
                    name VARCHAR(150) NOT NULL,
                    asset_type VARCHAR(50) NOT NULL DEFAULT 'HVAC',
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    tonnage_tr DOUBLE PRECISION,
                    phase_count SMALLINT NOT NULL DEFAULT 1,
                    voltage_basis VARCHAR(20) NOT NULL DEFAULT 'line_to_neutral',
                    inverter BOOLEAN NOT NULL DEFAULT FALSE,
                    rated_power_kw DOUBLE PRECISION,
                    eer DOUBLE PRECISION,
                    power_at_50_kw DOUBLE PRECISION,
                    power_at_100_kw DOUBLE PRECISION,
                    measurement_mode VARCHAR(30) NOT NULL DEFAULT 'measured_submeter',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(branch_id, asset_code)
                );
                """
            )

            # ------------------------
            # Safe migrations for existing databases
            # ------------------------
            asset_migrations = [
                "ALTER TABLE assets ADD COLUMN IF NOT EXISTS quantity INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE assets ADD COLUMN IF NOT EXISTS tonnage_tr DOUBLE PRECISION",
                "ALTER TABLE assets ADD COLUMN IF NOT EXISTS phase_count SMALLINT NOT NULL DEFAULT 1",
                "ALTER TABLE assets ADD COLUMN IF NOT EXISTS voltage_basis VARCHAR(20) NOT NULL DEFAULT 'line_to_neutral'",
                "ALTER TABLE assets ADD COLUMN IF NOT EXISTS inverter BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE assets ADD COLUMN IF NOT EXISTS rated_power_kw DOUBLE PRECISION",
                "ALTER TABLE assets ADD COLUMN IF NOT EXISTS eer DOUBLE PRECISION",
                "ALTER TABLE assets ADD COLUMN IF NOT EXISTS power_at_50_kw DOUBLE PRECISION",
                "ALTER TABLE assets ADD COLUMN IF NOT EXISTS power_at_100_kw DOUBLE PRECISION",
                "ALTER TABLE assets ADD COLUMN IF NOT EXISTS measurement_mode VARCHAR(30) NOT NULL DEFAULT 'measured_submeter'",
            ]
            for statement in asset_migrations:
                cursor.execute(statement)

            # ------------------------
            # Devices
            # ------------------------
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id VARCHAR(100) PRIMARY KEY,
                    asset_id BIGINT REFERENCES assets(id) ON DELETE SET NULL,
                    device_type VARCHAR(50) NOT NULL DEFAULT 'energy_meter',
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    installed_at TIMESTAMPTZ,
                    last_seen_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            # ------------------------
            # Telemetry
            # ------------------------
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

            telemetry_migrations = [
                "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS interval_energy_kwh DOUBLE PRECISION",
                "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS interval_seconds DOUBLE PRECISION",
                "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS energy_reset BOOLEAN NOT NULL DEFAULT FALSE",
            ]
            for statement in telemetry_migrations:
                cursor.execute(statement)

            # ------------------------
            # Indexes
            # ------------------------
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_telemetry_device_time
                ON telemetry(device_id, recorded_at DESC, id DESC);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_telemetry_branch_time
                ON telemetry(branch_id, recorded_at DESC, id DESC);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_telemetry_asset_time
                ON telemetry(asset_id, recorded_at DESC, id DESC);
                """
            )

        conn.commit()


# ============================================================
# FASTAPI LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="Orchid Smart Energy Monitoring API",
    version="5.0.0",
    lifespan=lifespan,
)


# ============================================================
# PYDANTIC MODELS
# ============================================================

class EnergyReading(BaseModel):
    device_id: str = Field(..., min_length=1)
    branch_id: str = Field(..., min_length=1)
    asset_id: str = Field(..., min_length=1)
    voltage: float = Field(..., gt=0)
    current: float = Field(..., ge=0)
    power: float = Field(..., ge=0)
    energy_kwh: float = Field(..., ge=0)
    frequency: float = Field(..., gt=0)
    power_factor: float = Field(..., ge=0, le=1.05)


class CompanyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)


class BranchCreate(BaseModel):
    company_id: int
    name: str = Field(..., min_length=1, max_length=150)
    branch_code: str = Field(..., min_length=1, max_length=100)


class AssetCreate(BaseModel):
    branch_id: int
    asset_code: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=150)
    asset_type: str = Field(default="HVAC", max_length=50)
    quantity: int = Field(default=1, ge=1)
    tonnage_tr: Optional[float] = Field(default=None, gt=0)
    phase_count: int = Field(default=1, ge=1, le=3)
    voltage_basis: VoltageBasis = "line_to_neutral"
    inverter: bool = False
    rated_power_kw: Optional[float] = Field(default=None, gt=0)
    eer: Optional[float] = Field(default=None, gt=0)
    power_at_50_kw: Optional[float] = Field(default=None, gt=0)
    power_at_100_kw: Optional[float] = Field(default=None, gt=0)
    measurement_mode: MeasurementMode = "measured_submeter"


class DeviceCreate(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=100)
    asset_id: Optional[int] = None
    device_type: str = Field(default="energy_meter", max_length=50)


class AllocationItem(BaseModel):
    asset_id: int
    operating_factor: float = Field(default=1.0, ge=0, le=1)


class MainMeterAllocationRequest(BaseModel):
    total_active_power_kw: float = Field(..., ge=0)
    non_ac_power_kw: float = Field(default=0.0, ge=0)
    interval_hours: float = Field(default=1.0, gt=0)
    assets: List[AllocationItem] = Field(..., min_length=1)


# ============================================================
# BASIC ENDPOINTS
# ============================================================

@app.get("/")
def root():
    return {
        "status": "online",
        "service": "Orchid Smart Energy Monitoring API",
        "version": "5.0.0",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


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
            "test": result[0],
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Database connection failed: {exc}",
        )


# ============================================================
# COMPANY / BRANCH / ASSET / DEVICE
# ============================================================

@app.post("/api/v1/companies")
def create_company(data: CompanyCreate):
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO companies(name)
                    VALUES (%s)
                    ON CONFLICT(name) DO UPDATE SET name = EXCLUDED.name
                    RETURNING id, name;
                    """,
                    (data.name,),
                )
                row = cursor.fetchone()
            conn.commit()
        return {"status": "success", "company_id": row[0], "name": row[1]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create company: {exc}")


@app.post("/api/v1/branches")
def create_branch(data: BranchCreate):
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id FROM companies WHERE id = %s", (data.company_id,))
                if cursor.fetchone() is None:
                    raise HTTPException(status_code=404, detail="Company not found")

                cursor.execute(
                    """
                    INSERT INTO branches(company_id, name, branch_code)
                    VALUES (%s, %s, %s)
                    ON CONFLICT(branch_code) DO UPDATE SET
                        company_id = EXCLUDED.company_id,
                        name = EXCLUDED.name
                    RETURNING id, company_id, name, branch_code;
                    """,
                    (data.company_id, data.name, data.branch_code),
                )
                row = cursor.fetchone()
            conn.commit()
        return {
            "status": "success",
            "branch_id": row[0],
            "company_id": row[1],
            "name": row[2],
            "branch_code": row[3],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create branch: {exc}")


@app.post("/api/v1/assets")
def create_asset(data: AssetCreate):
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id FROM branches WHERE id = %s", (data.branch_id,))
                if cursor.fetchone() is None:
                    raise HTTPException(status_code=404, detail="Branch not found")

                cursor.execute(
                    """
                    INSERT INTO assets(
                        branch_id,
                        asset_code,
                        name,
                        asset_type,
                        quantity,
                        tonnage_tr,
                        phase_count,
                        voltage_basis,
                        inverter,
                        rated_power_kw,
                        eer,
                        power_at_50_kw,
                        power_at_100_kw,
                        measurement_mode
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT(branch_id, asset_code) DO UPDATE SET
                        name = EXCLUDED.name,
                        asset_type = EXCLUDED.asset_type,
                        quantity = EXCLUDED.quantity,
                        tonnage_tr = EXCLUDED.tonnage_tr,
                        phase_count = EXCLUDED.phase_count,
                        voltage_basis = EXCLUDED.voltage_basis,
                        inverter = EXCLUDED.inverter,
                        rated_power_kw = EXCLUDED.rated_power_kw,
                        eer = EXCLUDED.eer,
                        power_at_50_kw = EXCLUDED.power_at_50_kw,
                        power_at_100_kw = EXCLUDED.power_at_100_kw,
                        measurement_mode = EXCLUDED.measurement_mode
                    RETURNING
                        id,
                        branch_id,
                        asset_code,
                        name,
                        asset_type,
                        quantity,
                        tonnage_tr,
                        phase_count,
                        voltage_basis,
                        inverter,
                        rated_power_kw,
                        eer,
                        power_at_50_kw,
                        power_at_100_kw,
                        measurement_mode;
                    """,
                    (
                        data.branch_id,
                        data.asset_code,
                        data.name,
                        data.asset_type,
                        data.quantity,
                        data.tonnage_tr,
                        data.phase_count,
                        data.voltage_basis,
                        data.inverter,
                        data.rated_power_kw,
                        data.eer,
                        data.power_at_50_kw,
                        data.power_at_100_kw,
                        data.measurement_mode,
                    ),
                )
                row = cursor.fetchone()
            conn.commit()

        return {
            "status": "success",
            "asset_id": row[0],
            "branch_id": row[1],
            "asset_code": row[2],
            "name": row[3],
            "asset_type": row[4],
            "quantity": row[5],
            "tonnage_tr": row[6],
            "phase_count": row[7],
            "voltage_basis": row[8],
            "inverter": row[9],
            "rated_power_kw": row[10],
            "eer": row[11],
            "power_at_50_kw": row[12],
            "power_at_100_kw": row[13],
            "measurement_mode": row[14],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create/update asset: {exc}")


@app.post("/api/v1/devices")
def create_device(data: DeviceCreate):
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                if data.asset_id is not None:
                    cursor.execute("SELECT id FROM assets WHERE id = %s", (data.asset_id,))
                    if cursor.fetchone() is None:
                        raise HTTPException(status_code=404, detail="Asset not found")

                cursor.execute(
                    """
                    INSERT INTO devices(device_id, asset_id, device_type)
                    VALUES (%s, %s, %s)
                    ON CONFLICT(device_id) DO UPDATE SET
                        asset_id = COALESCE(EXCLUDED.asset_id, devices.asset_id),
                        device_type = EXCLUDED.device_type
                    RETURNING device_id, asset_id, device_type, active;
                    """,
                    (data.device_id, data.asset_id, data.device_type),
                )
                row = cursor.fetchone()
            conn.commit()
        return {
            "status": "success",
            "device_id": row[0],
            "asset_id": row[1],
            "device_type": row[2],
            "active": row[3],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create/update device: {exc}")


@app.get("/api/v1/structure")
def get_structure():
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        c.id, c.name,
                        b.id, b.name, b.branch_code,
                        a.id, a.asset_code, a.name, a.asset_type, a.active,
                        a.quantity, a.tonnage_tr, a.phase_count, a.voltage_basis,
                        a.inverter, a.rated_power_kw, a.eer,
                        a.power_at_50_kw, a.power_at_100_kw, a.measurement_mode,
                        d.device_id, d.device_type, d.active, d.last_seen_at
                    FROM companies c
                    LEFT JOIN branches b ON b.company_id = c.id
                    LEFT JOIN assets a ON a.branch_id = b.id
                    LEFT JOIN devices d ON d.asset_id = a.id
                    ORDER BY c.id, b.id, a.id, d.device_id;
                    """
                )
                rows = cursor.fetchall()

        companies = {}
        for row in rows:
            (
                company_id, company_name,
                branch_id, branch_name, branch_code,
                asset_id, asset_code, asset_name, asset_type, asset_active,
                quantity, tonnage_tr, phase_count, voltage_basis,
                inverter, rated_power_kw, eer,
                power_at_50_kw, power_at_100_kw, measurement_mode,
                device_id, device_type, device_active, last_seen_at,
            ) = row

            company = companies.setdefault(
                company_id,
                {"company_id": company_id, "company_name": company_name, "branches": {}},
            )

            if branch_id is None:
                continue

            branch = company["branches"].setdefault(
                branch_id,
                {
                    "branch_id": branch_id,
                    "branch_name": branch_name,
                    "branch_code": branch_code,
                    "assets": {},
                },
            )

            if asset_id is None:
                continue

            asset = branch["assets"].setdefault(
                asset_id,
                {
                    "asset_id": asset_id,
                    "asset_code": asset_code,
                    "asset_name": asset_name,
                    "asset_type": asset_type,
                    "active": asset_active,
                    "quantity": quantity,
                    "tonnage_tr": tonnage_tr,
                    "phase_count": phase_count,
                    "voltage_basis": voltage_basis,
                    "inverter": inverter,
                    "rated_power_kw": rated_power_kw,
                    "eer": eer,
                    "power_at_50_kw": power_at_50_kw,
                    "power_at_100_kw": power_at_100_kw,
                    "measurement_mode": measurement_mode,
                    "devices": [],
                },
            )

            if device_id is not None:
                asset["devices"].append(
                    {
                        "device_id": device_id,
                        "device_type": device_type,
                        "active": device_active,
                        "last_seen_at": last_seen_at,
                    }
                )

        result = []
        for company in companies.values():
            company["branches"] = list(company["branches"].values())
            for branch in company["branches"]:
                branch["assets"] = list(branch["assets"].values())
            result.append(company)

        return {"status": "success", "companies": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load structure: {exc}")


# ============================================================
# TELEMETRY INGESTION
# ============================================================

@app.post("/api/v1/telemetry")
def receive_telemetry(reading: EnergyReading):
    recorded_at = datetime.now(timezone.utc)

    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                # Find the registered asset.
                cursor.execute(
                    """
                    SELECT
                        a.id,
                        a.phase_count,
                        a.voltage_basis,
                        a.measurement_mode
                    FROM assets a
                    JOIN branches b ON b.id = a.branch_id
                    WHERE b.branch_code = %s
                      AND a.asset_code = %s
                    LIMIT 1;
                    """,
                    (reading.branch_id, reading.asset_id),
                )
                asset_row = cursor.fetchone()

                matched_asset_id = asset_row[0] if asset_row else None
                phase_count = int(asset_row[1]) if asset_row else 1
                voltage_basis = asset_row[2] if asset_row else "line_to_neutral"
                measurement_mode = asset_row[3] if asset_row else "measured_submeter"

                # Previous cumulative meter value for interval energy.
                cursor.execute(
                    """
                    SELECT energy_kwh, recorded_at
                    FROM telemetry
                    WHERE device_id = %s
                    ORDER BY recorded_at DESC, id DESC
                    LIMIT 1;
                    """,
                    (reading.device_id,),
                )
                previous = cursor.fetchone()

                interval_energy_kwh = None
                interval_seconds = None
                energy_reset = False

                if previous is not None:
                    previous_energy = float(previous[0])
                    previous_time = previous[1]
                    seconds = (recorded_at - previous_time).total_seconds()

                    if seconds > 0 and reading.energy_kwh >= previous_energy:
                        interval_seconds = seconds
                        interval_energy_kwh = reading.energy_kwh - previous_energy
                    elif seconds > 0 and reading.energy_kwh < previous_energy:
                        # Meter restart/reset. Do not manufacture negative energy.
                        interval_seconds = seconds
                        interval_energy_kwh = None
                        energy_reset = True

                # Register/update the device.
                cursor.execute(
                    """
                    INSERT INTO devices(
                        device_id,
                        asset_id,
                        device_type,
                        last_seen_at
                    )
                    VALUES(%s, %s, 'energy_meter', %s)
                    ON CONFLICT(device_id) DO UPDATE SET
                        asset_id = COALESCE(devices.asset_id, EXCLUDED.asset_id),
                        last_seen_at = EXCLUDED.last_seen_at;
                    """,
                    (reading.device_id, matched_asset_id, recorded_at),
                )

                # Store raw telemetry.
                cursor.execute(
                    """
                    INSERT INTO telemetry(
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
                        interval_energy_kwh,
                        interval_seconds,
                        energy_reset
                    )
                    VALUES(
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s
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
                        recorded_at,
                        interval_energy_kwh,
                        interval_seconds,
                        energy_reset,
                    ),
                )
                telemetry_id = cursor.fetchone()[0]

            conn.commit()

        formula_power_kw = active_power_from_vi_pf(
            reading.voltage,
            reading.current,
            reading.power_factor,
            phase_count=phase_count,
            voltage_basis=voltage_basis,
        )
        meter_power_kw = reading.power / 1000.0
        difference_percent = None
        if meter_power_kw > 0:
            difference_percent = ((formula_power_kw - meter_power_kw) / meter_power_kw) * 100.0

        return {
            "status": "received",
            "stored": True,
            "telemetry_id": telemetry_id,
            "device_id": reading.device_id,
            "asset_id": reading.asset_id,
            "measurement": {
                "mode": measurement_mode,
                "source": "energy_meter",
                "meter_power_kw": round(meter_power_kw, 6),
                "vi_pf_formula_power_kw": round(formula_power_kw, 6),
                "power_difference_percent": round(difference_percent, 3) if difference_percent is not None else None,
                "cumulative_energy_kwh": reading.energy_kwh,
                "interval_energy_kwh": round(interval_energy_kwh, 6) if interval_energy_kwh is not None else None,
                "interval_seconds": round(interval_seconds, 3) if interval_seconds is not None else None,
                "energy_reset_detected": energy_reset,
                "formula": (
                    "P = V × I × PF / 1000 for single-phase"
                    if phase_count == 1
                    else (
                        "P = √3 × VLL × I × PF / 1000 for balanced three-phase"
                        if voltage_basis == "line_to_line"
                        else "P = 3 × VLN × I × PF / 1000 for balanced three-phase"
                    )
                ),
                "primary_energy_rule": "Actual energy = cumulative meter kWh difference over time; do not sum cumulative kWh values.",
            },
            "recorded_at": recorded_at.isoformat(),
        }
    except Exception as exc:
        print("DATABASE INSERT ERROR:")
        print(str(exc))
        raise HTTPException(status_code=500, detail="Failed to store telemetry")


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
                        received_at,
                        interval_energy_kwh,
                        interval_seconds,
                        energy_reset
                    FROM telemetry
                    WHERE device_id = %s
                    ORDER BY recorded_at DESC, id DESC
                    LIMIT 1;
                    """,
                    (device_id,),
                )
                row = cursor.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="No telemetry found for this device")

        return {
            "id": row[0],
            "device_id": row[1],
            "branch_id": row[2],
            "asset_id": row[3],
            "voltage": row[4],
            "current": row[5],
            "power": row[6],
            "power_kw": round(row[6] / 1000.0, 6),
            "energy_kwh": row[7],
            "frequency": row[8],
            "power_factor": row[9],
            "recorded_at": row[10],
            "received_at": row[11],
            "interval_energy_kwh": row[12],
            "interval_seconds": row[13],
            "energy_reset": row[14],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read telemetry: {exc}")


# ============================================================
# LIVE ENGINEERING VIEW
# ============================================================

@app.get("/api/v1/energy/live/{device_id}")
def live_energy(device_id: str):
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        t.id,
                        t.device_id,
                        t.branch_id,
                        t.asset_id,
                        t.voltage,
                        t.current,
                        t.power,
                        t.energy_kwh,
                        t.frequency,
                        t.power_factor,
                        t.recorded_at,
                        t.interval_energy_kwh,
                        t.interval_seconds,
                        t.energy_reset,
                        a.tonnage_tr,
                        a.phase_count,
                        a.voltage_basis,
                        a.inverter,
                        a.rated_power_kw,
                        a.eer,
                        a.measurement_mode
                    FROM telemetry t
                    LEFT JOIN branches b
                        ON b.branch_code = t.branch_id
                    LEFT JOIN assets a
                        ON a.branch_id = b.id
                       AND a.asset_code = t.asset_id
                    WHERE t.device_id = %s
                    ORDER BY t.recorded_at DESC, t.id DESC
                    LIMIT 1;
                    """,
                    (device_id,),
                )
                row = cursor.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="No telemetry found for this device")

        (
            _id,
            _device_id,
            branch_id,
            asset_code,
            voltage,
            current,
            power,
            energy_kwh,
            frequency,
            pf,
            recorded_at,
            interval_energy,
            interval_seconds,
            energy_reset,
            tonnage_tr,
            phase_count,
            voltage_basis,
            inverter,
            rated_power_kw,
            eer,
            measurement_mode,
        ) = row

        # Recalculate from electrical inputs only as a cross-check.
        formula_kw = active_power_from_vi_pf(
            voltage,
            current,
            pf,
            phase_count=int(phase_count or 1),
            voltage_basis=voltage_basis or "line_to_neutral",
        )
        meter_kw = power / 1000.0

        return {
            "device_id": device_id,
            "branch_id": branch_id,
            "asset_id": asset_code,
            "timestamp": recorded_at,
            "ac_metadata": {
                "tonnage_tr": tonnage_tr,
                "phase_count": phase_count,
                "voltage_basis": voltage_basis,
                "inverter": inverter,
                "rated_power_kw": rated_power_kw,
                "eer": eer,
                "measurement_mode": measurement_mode,
            },
            "real_time": {
                "voltage_v": voltage,
                "current_a": current,
                "power_kw_meter": round(meter_kw, 6),
                "power_kw_vi_pf_check": round(formula_kw, 6),
                "cumulative_energy_kwh": energy_kwh,
                "recent_interval_energy_kwh": interval_energy,
                "recent_interval_seconds": interval_seconds,
                "frequency_hz": frequency,
                "power_factor": pf,
                "energy_reset_detected": energy_reset,
            },
            "formula_rule": (
                "Use the energy meter's active power/kWh as primary measured values. "
                "V × I × PF is a verification calculation, not a replacement for a real meter."
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to calculate live energy: {exc}")


# ============================================================
# ESTIMATED ENERGY FOR AN AC WITHOUT AN INDIVIDUAL SUBMETER
# ============================================================

@app.get("/api/v1/energy/estimate/{asset_id}")
def estimate_asset_energy(
    asset_id: int,
    hours: float = Query(default=1.0, gt=0, le=24),
    operating_factor: float = Query(default=1.0, ge=0, le=1),
    tariff_per_kwh: float = Query(default=8.00, gt=0),
):
    """Estimate energy when individual AC electrical measurement is unavailable.

    This endpoint is explicitly an ESTIMATE. It does not infer individual AC
    consumption exactly from the school's main EB meter.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        id,
                        name,
                        asset_code,
                        quantity,
                        tonnage_tr,
                        inverter,
                        rated_power_kw,
                        eer,
                        power_at_50_kw,
                        power_at_100_kw,
                        measurement_mode
                    FROM assets
                    WHERE id = %s;
                    """,
                    (asset_id,),
                )
                asset = cursor.fetchone()

        if asset is None:
            raise HTTPException(status_code=404, detail="Asset not found")

        (
            _id,
            name,
            asset_code,
            quantity,
            tonnage_tr,
            inverter,
            rated_power_kw,
            eer,
            power_at_50_kw,
            power_at_100_kw,
            measurement_mode,
        ) = asset

        base_kw, source = rated_input_power_kw(
            tonnage_tr,
            rated_power_kw,
            eer,
        )

        interpolation = False
        interpolation_note = None

        # If manufacturer full/50% input powers exist for an inverter,
        # use a transparent linear estimate between those two rated points
        # for operating factors between 0.5 and 1.0.
        if (
            inverter
            and power_at_50_kw is not None
            and power_at_100_kw is not None
            and 0.5 <= operating_factor <= 1.0
        ):
            fraction = (operating_factor - 0.5) / 0.5
            base_kw = float(power_at_50_kw) + (
                float(power_at_100_kw) - float(power_at_50_kw)
            ) * fraction
            source = "linear_estimate_between_manufacturer_50_and_100_percent_input_power"
            interpolation = True
            interpolation_note = (
                "Estimated interpolation only; actual inverter power must be measured for exact consumption."
            )

        estimated_kw = base_kw * int(quantity) * operating_factor
        estimated_kwh = estimated_kw * hours
        estimated_cost = estimated_kwh * tariff_per_kwh

        return {
            "asset_id": asset_id,
            "asset_code": asset_code,
            "asset_name": name,
            "mode": "ESTIMATED",
            "measurement_mode_configured": measurement_mode,
            "quantity": quantity,
            "tonnage_tr": tonnage_tr,
            "inverter": inverter,
            "operating_factor": operating_factor,
            "hours": hours,
            "rated_input_power_basis_kw": round(base_kw, 6),
            "estimated_power_kw": round(estimated_kw, 6),
            "estimated_energy_kwh": round(estimated_kwh, 6),
            "tariff_per_kwh": tariff_per_kwh,
            "estimated_cost": round(estimated_cost, 2),
            "formula_source": source,
            "formula": (
                "E = quantity × input_power_kW × operating_factor × hours"
            ),
            "cooling_to_input_rule": (
                "input_power_kW = (3.517 × TR) / EER when rated input power is unavailable"
            ),
            "interpolation_used": interpolation,
            "note": interpolation_note,
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to estimate asset energy: {exc}")


# ============================================================
# TOP-DOWN ALLOCATION FROM A SINGLE EB/MAIN METER READING
# ============================================================

@app.post("/api/v1/energy/main-meter-allocation")
def main_meter_allocation(request: MainMeterAllocationRequest):
    """Estimate AC contributions when only one aggregate main-meter power value exists.

    This is a load-allocation MODEL, not an exact disaggregation formula.
    Exact per-AC energy requires submetering or other validated measurement.
    """
    available_ac_power_kw = max(
        request.total_active_power_kw - request.non_ac_power_kw,
        0.0,
    )

    with get_connection() as conn:
        with conn.cursor() as cursor:
            results = []
            nominal_total = 0.0

            for item in request.assets:
                cursor.execute(
                    """
                    SELECT
                        id,
                        asset_code,
                        name,
                        quantity,
                        tonnage_tr,
                        rated_power_kw,
                        eer
                    FROM assets
                    WHERE id = %s;
                    """,
                    (item.asset_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Asset {item.asset_id} not found",
                    )

                (
                    asset_db_id,
                    asset_code,
                    name,
                    quantity,
                    tonnage_tr,
                    rated_power_kw,
                    eer,
                ) = row

                try:
                    base_kw, source = rated_input_power_kw(
                        tonnage_tr,
                        rated_power_kw,
                        eer,
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Asset {item.asset_id}: {exc}",
                    )

                nominal_kw = base_kw * int(quantity) * item.operating_factor
                nominal_total += nominal_kw

                results.append(
                    {
                        "asset_id": asset_db_id,
                        "asset_code": asset_code,
                        "asset_name": name,
                        "nominal_estimated_power_kw": nominal_kw,
                        "formula_source": source,
                    }
                )

    if nominal_total <= 0:
        raise HTTPException(
            status_code=400,
            detail="Total estimated AC load is zero; check rated power/EER and operating factors.",
        )

    # Never inflate an AC estimate above the main meter available AC power.
    scale_factor = min(1.0, available_ac_power_kw / nominal_total)

    allocated_total_kw = 0.0

    for item in results:
        allocated_kw = item["nominal_estimated_power_kw"] * scale_factor
        item["allocated_power_kw"] = round(allocated_kw, 6)
        item["allocated_energy_kwh"] = round(
            allocated_kw * request.interval_hours,
            6,
        )
        allocated_total_kw += allocated_kw

    return {
        "mode": "MAIN_METER_ALLOCATED",
        "warning": (
            "Individual AC consumption is estimated because the main EB meter measures the aggregate load. "
            "A submeter/energy meter per AC or feeder is required for exact per-AC consumption."
        ),
        "main_meter_power_kw": request.total_active_power_kw,
        "assumed_non_ac_power_kw": request.non_ac_power_kw,
        "available_ac_power_kw": round(available_ac_power_kw, 6),
        "nominal_estimated_ac_power_kw": round(nominal_total, 6),
        "allocation_scale_factor": round(scale_factor, 6),
        "allocated_ac_power_kw": round(allocated_total_kw, 6),
        "unallocated_or_other_power_kw": round(
            max(request.total_active_power_kw - request.non_ac_power_kw - allocated_total_kw, 0.0),
            6,
        ),
        "interval_hours": request.interval_hours,
        "assets": results,
    }


# ============================================================
# HOURLY ENERGY
# ============================================================

@app.get("/api/v1/energy/hourly/{device_id}")
def hourly_energy(
    device_id: str,
    report_date: Optional[date] = Query(default=None),
    tariff_per_kwh: float = Query(default=8.00, gt=0),
):
    if report_date is None:
        report_date = datetime.now(INDIA_TZ).date()

    start_local = datetime.combine(report_date, time.min, tzinfo=INDIA_TZ)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        date_trunc('hour', recorded_at AT TIME ZONE 'Asia/Kolkata') AS hour_local,
                        COALESCE(SUM(interval_energy_kwh), 0) AS interval_energy,
                        AVG(power) AS average_power_w,
                        MAX(power) AS peak_power_w,
                        AVG(current) AS average_current_a,
                        COUNT(*) AS readings,
                        BOOL_OR(energy_reset) AS had_reset
                    FROM telemetry
                    WHERE device_id = %s
                      AND recorded_at >= %s
                      AND recorded_at < %s
                    GROUP BY hour_local
                    ORDER BY hour_local;
                    """,
                    (device_id, start_utc, end_utc),
                )
                rows = cursor.fetchall()

        hourly_data = []
        total_energy = 0.0

        for row in rows:
            hour_local, consumed, avg_power, peak_power, avg_current, readings, had_reset = row
            consumed = float(consumed or 0.0)
            total_energy += consumed
            hour_start = hour_local.replace(tzinfo=INDIA_TZ)

            hourly_data.append(
                {
                    "hour_start": hour_start.isoformat(),
                    "energy_consumed_kwh": round(consumed, 6),
                    "estimated_cost": round(consumed * tariff_per_kwh, 2),
                    "average_power_w": round(float(avg_power or 0.0), 2),
                    "peak_power_w": round(float(peak_power or 0.0), 2),
                    "average_current_a": round(float(avg_current or 0.0), 3),
                    "readings": int(readings),
                    "meter_reset_detected": bool(had_reset),
                }
            )

        return {
            "device_id": device_id,
            "report_date": report_date.isoformat(),
            "tariff_per_kwh": tariff_per_kwh,
            "total_energy_kwh": round(total_energy, 6),
            "total_estimated_cost": round(total_energy * tariff_per_kwh, 2),
            "hours_with_data": len(hourly_data),
            "calculation": "Sum of cumulative-meter energy deltas between successive readings; reset intervals are excluded.",
            "data": hourly_data,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to calculate hourly energy: {exc}")


# ============================================================
# DAILY ENERGY
# ============================================================

@app.get("/api/v1/energy/daily/{device_id}")
def daily_energy(
    device_id: str,
    days: int = Query(default=7, ge=1, le=90),
    tariff_per_kwh: float = Query(default=8.00, gt=0),
):
    today_local = datetime.now(INDIA_TZ).date()
    start_date = today_local - timedelta(days=days - 1)
    start_local = datetime.combine(start_date, time.min, tzinfo=INDIA_TZ)
    end_local = datetime.combine(today_local, time.min, tzinfo=INDIA_TZ) + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        (recorded_at AT TIME ZONE 'Asia/Kolkata')::date AS local_date,
                        COALESCE(SUM(interval_energy_kwh), 0) AS interval_energy,
                        AVG(power) AS average_power_w,
                        MAX(power) AS peak_power_w,
                        COUNT(*) AS readings,
                        BOOL_OR(energy_reset) AS had_reset
                    FROM telemetry
                    WHERE device_id = %s
                      AND recorded_at >= %s
                      AND recorded_at < %s
                    GROUP BY local_date
                    ORDER BY local_date;
                    """,
                    (device_id, start_utc, end_utc),
                )
                rows = cursor.fetchall()

        data = []
        total_energy = 0.0

        for row in rows:
            local_date, consumed, avg_power, peak_power, readings, had_reset = row
            consumed = float(consumed or 0.0)
            total_energy += consumed
            data.append(
                {
                    "date": local_date.isoformat(),
                    "energy_consumed_kwh": round(consumed, 6),
                    "estimated_cost": round(consumed * tariff_per_kwh, 2),
                    "average_power_w": round(float(avg_power or 0.0), 2),
                    "peak_power_w": round(float(peak_power or 0.0), 2),
                    "readings": int(readings),
                    "meter_reset_detected": bool(had_reset),
                }
            )

        return {
            "device_id": device_id,
            "start_date": start_date.isoformat(),
            "end_date": today_local.isoformat(),
            "days_requested": days,
            "tariff_per_kwh": tariff_per_kwh,
            "total_energy_kwh": round(total_energy, 6),
            "total_estimated_cost": round(total_energy * tariff_per_kwh, 2),
            "calculation": "Sum of successive cumulative-meter energy deltas.",
            "data": data,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to calculate daily energy: {exc}")


# ============================================================
# DAILY SUMMARY
# ============================================================

@app.get("/api/v1/energy/summary/{device_id}")
def energy_summary(
    device_id: str,
    report_date: Optional[date] = Query(default=None),
    tariff_per_kwh: float = Query(default=8.00, gt=0),
):
    if report_date is None:
        report_date = datetime.now(INDIA_TZ).date()

    start_local = datetime.combine(report_date, time.min, tzinfo=INDIA_TZ)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS readings,
                        COALESCE(SUM(interval_energy_kwh), 0) AS interval_energy,
                        MIN(energy_kwh) AS first_energy_kwh,
                        MAX(energy_kwh) AS last_energy_kwh,
                        AVG(power) AS average_power_w,
                        MAX(power) AS peak_power_w,
                        AVG(current) AS average_current_a,
                        MAX(current) AS peak_current_a,
                        MIN(recorded_at) AS first_recorded_at,
                        MAX(recorded_at) AS last_recorded_at,
                        BOOL_OR(energy_reset) AS had_reset
                    FROM telemetry
                    WHERE device_id = %s
                      AND recorded_at >= %s
                      AND recorded_at < %s;
                    """,
                    (device_id, start_utc, end_utc),
                )
                row = cursor.fetchone()

        readings = int(row[0])
        if readings == 0:
            return {
                "device_id": device_id,
                "report_date": report_date.isoformat(),
                "message": "No telemetry data found for this date.",
                "readings": 0,
            }

        interval_energy = float(row[1] or 0.0)
        first_energy = float(row[2])
        last_energy = float(row[3])
        cumulative_delta = max(last_energy - first_energy, 0.0)
        estimated_cost = interval_energy * tariff_per_kwh

        return {
            "device_id": device_id,
            "report_date": report_date.isoformat(),
            "readings": readings,
            "energy_start_kwh": round(first_energy, 6),
            "energy_end_kwh": round(last_energy, 6),
            "energy_consumed_kwh": round(interval_energy, 6),
            "cumulative_delta_cross_check_kwh": round(cumulative_delta, 6),
            "average_power_w": round(float(row[4] or 0.0), 2),
            "peak_power_w": round(float(row[5] or 0.0), 2),
            "average_current_a": round(float(row[6] or 0.0), 3),
            "peak_current_a": round(float(row[7] or 0.0), 3),
            "estimated_cost": round(estimated_cost, 2),
            "first_recorded_at": row[8],
            "last_recorded_at": row[9],
            "meter_reset_detected": bool(row[10]),
            "calculation": "Primary energy = sum of successive cumulative-kWh deltas; cumulative end-start is returned as a cross-check.",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to calculate energy summary: {exc}")
