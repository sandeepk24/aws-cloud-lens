"""API route modules."""

from fastapi import APIRouter

from app.api.routes import graph, health, inventory

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(inventory.router)
api_router.include_router(graph.router)
