"""
One-time setup/diagnostic script.

Run this the moment MONDAY_API_TOKEN exists:

    python list_boards.py

It lists every board the token can see, with board IDs and each column's id/title/type, so you
can (a) sanity-check that "Work Orders" and "Deals" are visible, and (b) optionally copy their
IDs into .env as MONDAY_WORK_ORDERS_BOARD_ID / MONDAY_DEALS_BOARD_ID. The app also auto-detects
these two boards by name if you leave the IDs blank, so this script is diagnostic more than
strictly required.
"""
import sys

import config
import monday_client
from monday_client import MondayError


def main() -> int:
    if not config.MONDAY_API_TOKEN:
        print("MONDAY_API_TOKEN is not set. Copy .env.example to .env, generate a token in")
        print("Monday.com (Avatar -> Admin/Developer -> API -> Generate token), and set it there.")
        return 1

    try:
        boards = monday_client.list_boards()
    except MondayError as exc:
        print(f"Could not reach Monday.com: {exc}")
        return 1

    if not boards:
        print("No boards visible to this token. Check that the token's user has board access.")
        return 1

    print(f"Found {len(boards)} board(s):\n")
    for board in boards:
        flag = ""
        normalized_name = monday_client._normalize_for_match(board["name"])
        if config.WORK_ORDERS_BOARD_NAME_HINT in normalized_name:
            flag = "  <-- looks like the Work Orders board"
        elif config.DEALS_BOARD_NAME_HINT in normalized_name:
            flag = "  <-- looks like the Deals board"

        print(f"Board: {board['name']}{flag}")
        print(f"  id:    {board['id']}")
        print(f"  state: {board['state']}")
        print("  columns:")
        for col in board["columns"]:
            print(f"    - id={col['id']!r:30} type={col['type']:15} title={col['title']!r}")
        print()

    print("If auto-detection above looks wrong, set MONDAY_WORK_ORDERS_BOARD_ID and")
    print("MONDAY_DEALS_BOARD_ID explicitly in .env using the 'id:' values printed above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
