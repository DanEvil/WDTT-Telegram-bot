#!/usr/bin/env python3
"""
VK Hash Lazy Initialization Service

This service runs alongside the bot and handles lazy VK hash creation
when users first connect via VLESS. It can be called via HTTP API from the Xray plugin.

Usage:
    python vk_hash_service.py --port 8083

Endpoints:
    POST /vk-hash/ensure  - Ensure VK hash exists for user (creates if needed)
    GET  /vk-hash/status  - Check VK hash status for user
"""

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

from aiohttp import web
from aiohttp.web import Request, Response, json_response

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import (
    get_customer_by_wdtt_password,
    mark_vk_hash_initialized,
    is_vk_hash_initialized,
)
from wdtt import WdttClient, WdttError
from config import WDTT_BASE_URL, WDTT_USERNAME, WDTT_PASSWORD, WDTT_VERIFY_SSL

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Global WDTT client
wdtt_client: Optional[WdttClient] = None


@asynccontextmanager
async def get_wdtt_client():
    global wdtt_client
    if wdtt_client is None:
        wdtt_client = WdttClient(
            base_url=WDTT_BASE_URL,
            username=WDTT_USERNAME,
            password=WDTT_PASSWORD,
            verify_ssl=WDTT_VERIFY_SSL,
        )
    try:
        yield wdtt_client
    except WdttError:
        # Force re-login on next call
        wdtt_client = None
        raise


async def ensure_vk_hash(wdtt_password: str) -> dict:
    """
    Ensure VK hash exists for user. If not initialized, create VK call via panel.
    
    Returns: {"success": bool, "vk_hash": str, "initialized": bool, "error": str}
    """
    # Find customer by wdtt_password
    customer = await get_customer_by_wdtt_password(wdtt_password)
    if not customer:
        return {"success": False, "error": "Customer not found", "vk_hash": "", "initialized": False}
    
    telegram_id = customer["telegram_id"]
    
    # Check if already initialized
    if await is_vk_hash_initialized(telegram_id):
        # Get current vk_hash from panel
        async with get_wdtt_client() as client:
            users = await client.get_users()
            for user in users.get("users", []):
                if user.get("password") == wdtt_password:
                    return {
                        "success": True,
                        "vk_hash": user.get("vk_hash", ""),
                        "initialized": True,
                        "error": ""
                    }
        return {"success": True, "vk_hash": "", "initialized": True, "error": "Hash exists but not in panel response"}
    
    # Not initialized - create VK call via panel
    try:
        async with get_wdtt_client() as client:
            vk_hash = await client.create_vk_call(wdtt_password)
            
            # Mark as initialized in our database
            await mark_vk_hash_initialized(telegram_id, vk_hash)
            
            log.info(f"Created VK hash for user {telegram_id}: {vk_hash[:16]}...")
            
            return {
                "success": True,
                "vk_hash": vk_hash,
                "initialized": True,
                "error": ""
            }
    except WdttError as e:
        log.error(f"Failed to create VK hash for user {telegram_id}: {e}")
        return {"success": False, "error": str(e), "vk_hash": "", "initialized": False}


async def handle_ensure(request: Request) -> Response:
    """POST /vk-hash/ensure - Ensure VK hash for user by wdtt_password."""
    try:
        data = await request.json()
        wdtt_password = data.get("wdtt_password", "").strip()
        
        if not wdtt_password:
            return json_response({"success": False, "error": "wdtt_password required"}, status=400)
        
        result = await ensure_vk_hash(wdtt_password)
        return json_response(result)
    except Exception as e:
        log.exception("Error in ensure_vk_hash")
        return json_response({"success": False, "error": str(e)}, status=500)


async def handle_status(request: Request) -> Response:
    """GET /vk-hash/status?wdtt_password=... - Check VK hash status."""
    wdtt_password = request.query.get("wdtt_password", "").strip()
    
    if not wdtt_password:
        return json_response({"success": False, "error": "wdtt_password required"}, status=400)
    
    customer = await get_customer_by_wdtt_password(wdtt_password)
    if not customer:
        return json_response({"success": False, "error": "Customer not found"}, status=404)
    
    telegram_id = customer["telegram_id"]
    initialized = await is_vk_hash_initialized(telegram_id)
    
    vk_hash = ""
    if initialized:
        async with get_wdtt_client() as client:
            users = await client.get_users()
            for user in users.get("users", []):
                if user.get("password") == wdtt_password:
                    vk_hash = user.get("vk_hash", "")
                    break
    
    return json_response({
        "success": True,
        "initialized": initialized,
        "vk_hash": vk_hash,
        "telegram_id": telegram_id,
        "vk_hash_created_at": customer.get("vk_hash_created_at", 0)
    })


async def handle_health(request: Request) -> Response:
    """GET /health - Health check."""
    return json_response({"status": "ok", "service": "vk-hash-lazy-init"})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/vk-hash/ensure", handle_ensure)
    app.router.add_get("/vk-hash/status", handle_status)
    app.router.add_get("/health", handle_health)
    return app


async def init_wdtt_client():
    """Pre-initialize WDTT client."""
    global wdtt_client
    wdtt_client = WdttClient(
        base_url=WDTT_BASE_URL,
        username=WDTT_USERNAME,
        password=WDTT_PASSWORD,
        verify_ssl=WDTT_VERIFY_SSL,
    )
    try:
        await wdtt_client.login()
        log.info("WDTT client initialized successfully")
    except Exception as e:
        log.warning(f"WDTT client init failed (will retry on first request): {e}")


async def main():
    # Initialize WDTT client
    await init_wdtt_client()
    
    # Create and run web app
    app = create_app()
    
    port = int(os.getenv("VK_HASH_SERVICE_PORT", "8083"))
    host = os.getenv("VK_HASH_SERVICE_HOST", "0.0.0.0")
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    
    log.info(f"VK Hash Lazy Init Service running on http://{host}:{port}")
    
    try:
        await asyncio.Event().wait()  # Run forever
    except KeyboardInterrupt:
        pass
    finally:
        await runner.cleanup()
        if wdtt_client:
            await wdtt_client.close()


if __name__ == "__main__":
    asyncio.run(main())