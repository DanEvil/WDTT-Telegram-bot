#!/usr/bin/env python3
"""
Database Migration: Add lazy VK hash tracking columns

This migration adds columns to track VK hash initialization status
so we only create VK calls when user actually connects via VLESS.
"""

import asyncio
import aiosqlite
import os

DB_PATH = os.getenv("DB_PATH", "/app/data/bot.db")

async def migrate():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        # Check current schema
        cols = [r["name"] for r in (await (await db.execute("PRAGMA table_info(customers)")).fetchall())]
        print(f"Current columns: {cols}")
        
        # Add vk_hash_initialized column (0 = not initialized, 1 = initialized)
        if "vk_hash_initialized" not in cols:
            print("Adding vk_hash_initialized column...")
            await db.execute(
                "ALTER TABLE customers ADD COLUMN vk_hash_initialized INTEGER NOT NULL DEFAULT 0"
            )
            print("✓ Added vk_hash_initialized")
        else:
            print("vk_hash_initialized already exists")
        
        # Add vk_hash_created_at column to track when VK hash was created
        if "vk_hash_created_at" not in cols:
            print("Adding vk_hash_created_at column...")
            await db.execute(
                "ALTER TABLE customers ADD COLUMN vk_hash_created_at INTEGER NOT NULL DEFAULT 0"
            )
            print("✓ Added vk_hash_created_at")
        else:
            print("vk_hash_created_at already exists")
            
        # Add index for vk_hash_initialized for fast queries
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_customers_vk_hash_initialized ON customers(vk_hash_initialized)"
        )
        
        await db.commit()
        print("\n✅ Migration completed successfully")

if __name__ == "__main__":
    asyncio.run(migrate())