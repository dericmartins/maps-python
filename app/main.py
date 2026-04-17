from __future__ import annotations

import json
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from scgraph import GeoGraph

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

CACHE_DIR = os.getenv("SCGRAPH_CACHE_DIR", "/tmp/scgraph-cache")
GEOGRAPH_NAME = os.getenv("SCGRAPH_GEOGRAPH", "marnet")
OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org")
OSRM_PROFILE = os.getenv("OSRM_PROFILE", "driving")

_graph_lock = Lock()
_geograph: GeoGraph | None = None


PORTS = [
    {"name": "Porto de Itajaí", "country": "BR", "latitude": -26.9017, "longitude": -48.6503},
    {"name": "Porto de Navegantes", "country": "BR", "latitude": -26.8941, "longitude": -48.6546},
    {"name": "Porto de São Francisco do Sul", "country": "BR", "latitude": -26.2434, "longitude": -48.6383},
    {"name": "Porto de Paranaguá", "country": "BR", "latitude": -25.5205, "longitude": -48.5086},
    {"name": "Porto de Santos", "country": "BR", "latitude": -23.9618, "longitude": -46.3280},
    {"name": "Port of Cartagena", "country": "CO", "latitude": 10.3997, "longitude": -75.5144},
    {"name": "Port of Barranquilla", "country": "CO", "latitude": 10.9685, "longitude": -74.7813},
    {"name": "Port of Buenaventura", "country": "CO", "latitude": 3.8802, "longitude": -77.0312},
    {"name": "Port of Colón", "country": "PA", "latitude": 9.3540, "longitude": -79.9000},
    {"name": "Port of Miami", "country": "US", "latitude": 25.7781, "longitude": -80.1794},
]


class Point(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    name: str | None = None


class RouteRequest(BaseModel):
    origin: Point
    destination: Point
    units: str = Field(default="km", pattern="^(km|m|mi|ft)$")


def get_geograph() -> GeoGraph:
    global _geograph

    if _geograph is None:
        with _graph_lock:
            if _geograph is None:
                os.makedirs(CACHE_DIR, exist_ok=True)
                _geograph = GeoGraph.load_geograph(
                    GEOGRAPH_NAME,
                    cache_dir=CACHE_DIR,
                )
    return _geograph


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    get_geograph()
    yield


app = FastAPI(
    title="SCGraph Multimodal Routing Service",
    version="0.3.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/static/index.html")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "geograph": GEOGRAPH_NAME,
        "cache_dir": CACHE_DIR,
        "ports_loaded": len(PORTS),
        "osrm_base_url": OSRM_BASE_URL,
        "osrm_profile": OSRM_PROFILE,
    }


@app.get("/ports")
def list_ports() -> dict[str, Any]:
    return {"ports": PORTS}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def convert_distance_from_km(distance_km: float, units: str) -> float:
    if units == "km":
        return distance_km
    if units == "m":
        return distance_km * 1000
    if units == "mi":
        return distance_km * 0.621371
    if units == "ft":
        return distance_km * 3280.84
    return distance_km


def nearest_port(point: Point) -> dict[str, Any]:
    ranked = sorted(
        PORTS,
        key=lambda port: haversine_km(
            point.latitude,
            point.longitude,
            port["latitude"],
            port["longitude"],
        ),
    )
    port = ranked[0].copy()
    port["distance_to_point_km"] = haversine_km(
        point.latitude,
        point.longitude,
        port["latitude"],
        port["longitude"],
    )
    return port


def make_road_leg_fallback(
    from_name: str,
    to_name: str,
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    units: str,
) -> dict[str, Any]:
    distance_km = haversine_km(origin_lat, origin_lon, dest_lat, dest_lon)
    distance = convert_distance_from_km(distance_km, units)

    coordinates = [
        [origin_lon, origin_lat],
        [dest_lon, dest_lat],
    ]

    return {
        "mode": "road",
        "from": from_name,
        "to": to_name,
        "distance": distance,
        "units": units,
        "provider": "fallback_haversine",
        "geojson": {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coordinates,
            },
            "properties": {
                "mode": "road",
                "from": from_name,
                "to": to_name,
                "distance": distance,
                "units": units,
                "provider": "fallback_haversine",
            },
        },
    }


def make_road_leg(
    from_name: str,
    to_name: str,
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    units: str,
) -> dict[str, Any]:
    coordinates = f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    query = urlencode(
        {
            "overview": "full",
            "geometries": "geojson",
            "steps": "false",
        }
    )

    url = f"{OSRM_BASE_URL}/route/v1/{OSRM_PROFILE}/{coordinates}?{query}"

    try:
        with urlopen(url, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if payload.get("code") != "Ok":
            raise RuntimeError(payload.get("message", "OSRM route error"))

        routes = payload.get("routes", [])
        if not routes:
            raise RuntimeError("OSRM did not return any route")

        route = routes[0]
        distance_m = float(route["distance"])
        geometry = route["geometry"]

        if units == "km":
            distance = distance_m / 1000
        elif units == "m":
            distance = distance_m
        elif units == "mi":
            distance = distance_m / 1609.344
        elif units == "ft":
            distance = distance_m * 3.28084
        else:
            distance = distance_m / 1000

        return {
            "mode": "road",
            "from": from_name,
            "to": to_name,
            "distance": distance,
            "units": units,
            "provider": "osrm",
            "geojson": {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "mode": "road",
                    "from": from_name,
                    "to": to_name,
                    "distance": distance,
                    "units": units,
                    "provider": "osrm",
                },
            },
        }

    except (HTTPError, URLError, TimeoutError, RuntimeError, KeyError, ValueError, json.JSONDecodeError):
        return make_road_leg_fallback(
            from_name=from_name,
            to_name=to_name,
            origin_lat=origin_lat,
            origin_lon=origin_lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            units=units,
        )


def make_sea_leg(
    from_port: dict[str, Any],
    to_port: dict[str, Any],
    units: str,
) -> dict[str, Any]:
    geograph = get_geograph()

    result = geograph.get_shortest_path(
        origin_node={
            "latitude": from_port["latitude"],
            "longitude": from_port["longitude"],
        },
        destination_node={
            "latitude": to_port["latitude"],
            "longitude": to_port["longitude"],
        },
        output_units=units,
    )

    coordinate_path = result.get("coordinate_path", [])
    geojson_coordinates = [[lon, lat] for lat, lon in coordinate_path]

    return {
        "mode": "sea",
        "from": from_port["name"],
        "to": to_port["name"],
        "distance": result.get("length"),
        "units": units,
        "provider": "scgraph",
        "path": coordinate_path,
        "geojson": {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": geojson_coordinates,
            },
            "properties": {
                "mode": "sea",
                "from": from_port["name"],
                "to": to_port["name"],
                "distance": result.get("length"),
                "units": units,
                "provider": "scgraph",
            },
        },
    }


@app.post("/route")
def maritime_route(payload: RouteRequest) -> dict[str, Any]:
    try:
        geograph = get_geograph()

        result = geograph.get_shortest_path(
            origin_node={
                "latitude": payload.origin.latitude,
                "longitude": payload.origin.longitude,
            },
            destination_node={
                "latitude": payload.destination.latitude,
                "longitude": payload.destination.longitude,
            },
            output_units=payload.units,
        )

        coordinate_path = result.get("coordinate_path", [])
        geojson_coordinates = [[lon, lat] for lat, lon in coordinate_path]

        return {
            "summary": {
                "distance": result.get("length"),
                "units": payload.units,
                "geograph": GEOGRAPH_NAME,
            },
            "path": coordinate_path,
            "geojson": {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": geojson_coordinates,
                },
                "properties": {
                    "distance": result.get("length"),
                    "units": payload.units,
                },
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/route/multimodal")
def multimodal_route(payload: RouteRequest) -> dict[str, Any]:
    try:
        origin_name = payload.origin.name or "Origem"
        destination_name = payload.destination.name or "Destino"

        origin_port = nearest_port(payload.origin)
        destination_port = nearest_port(payload.destination)

        road_start = make_road_leg(
            from_name=origin_name,
            to_name=origin_port["name"],
            origin_lat=payload.origin.latitude,
            origin_lon=payload.origin.longitude,
            dest_lat=origin_port["latitude"],
            dest_lon=origin_port["longitude"],
            units=payload.units,
        )

        sea_leg = make_sea_leg(
            from_port=origin_port,
            to_port=destination_port,
            units=payload.units,
        )

        road_end = make_road_leg(
            from_name=destination_port["name"],
            to_name=destination_name,
            origin_lat=destination_port["latitude"],
            origin_lon=destination_port["longitude"],
            dest_lat=payload.destination.latitude,
            dest_lon=payload.destination.longitude,
            units=payload.units,
        )

        total_distance = (
            float(road_start["distance"])
            + float(sea_leg["distance"])
            + float(road_end["distance"])
        )

        return {
            "summary": {
                "distance": total_distance,
                "units": payload.units,
                "geograph": GEOGRAPH_NAME,
            },
            "selected_ports": {
                "origin_port": origin_port,
                "destination_port": destination_port,
            },
            "legs": [road_start, sea_leg, road_end],
            "geojson": {
                "type": "FeatureCollection",
                "features": [
                    road_start["geojson"],
                    sea_leg["geojson"],
                    road_end["geojson"],
                ],
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc