"""
Prompt templates.

Tools are what the model can call; prompts are what the *user* can pick. In
Claude and Cursor they surface as ready-made actions, so they are the shortest
path from "installed this server" to "got an answer" -- which is exactly where
most installs die.

They also encode the three usage rules that a model otherwise has to infer from
the tool description and frequently gets wrong:

1. One flexible question is ONE call with a range, never one call per date.
2. `price_range_in_relation_to_other_periods` is the answer to "is this a good
   deal", and quoting a bare number without it is a worse answer.
3. Every combination is a billed request, so the spend is worth stating.

Each prompt is a plain function returning the user-facing text; FastMCP handles
the MCP wire format. Keep them short -- a prompt that lectures the model tends
to get paraphrased away.
"""

from __future__ import annotations

from fastmcp import FastMCP

from .schema_docs import document_params


def register_prompts(mcp: FastMCP) -> None:
    """Attach every prompt template to `mcp`."""

    @mcp.prompt(
        name="cheapest_dates",
        title="Find the cheapest dates to fly",
        description=(
            "Scan a whole month (or any date range) for the lowest fare on a "
            "route and say whether the winner is actually a good price."
        ),
    )
    @document_params
    def cheapest_dates(
        from_airport: str,
        to_airport: str,
        month: str,
    ) -> str:
        """
        Args:
            from_airport: Origin IATA code, e.g. "TLV".
            to_airport: Destination IATA code, e.g. "BUD".
            month: The month or range to scan, e.g. "October 2026".
        """
        return (
            f"Find the cheapest date to fly {from_airport} to {to_airport} in "
            f"{month}.\n\n"
            "Use search_oneway_flights ONCE with departure_date_from and "
            "departure_date_to covering the whole period -- do not call it once "
            "per date.\n\n"
            "Then tell me:\n"
            "- the cheapest date and fare, with the airline and number of stops\n"
            "- whether that fare is low, typical or high for this route, using "
            "price_range_in_relation_to_other_periods and Google's "
            "price_insights_low / price_insights_high band\n"
            "- how many dates were actually searched, from search_coverage, so "
            "I know whether the scan was exhaustive or sampled\n"
            "- what the search cost me, from api_usage\n\n"
            "Include the buy_link for the winner."
        )

    @mcp.prompt(
        name="compare_destinations",
        title="Compare destinations on price",
        description=(
            "Price several destinations from one origin over the same dates and "
            "rank them, to answer 'where can I go cheaply'."
        ),
    )
    @document_params
    def compare_destinations(
        from_airport: str,
        destinations: str,
        when: str,
    ) -> str:
        """
        Args:
            from_airport: Origin IATA code, e.g. "TLV".
            destinations: Comma-separated IATA codes, e.g. "BUD,VIE,ATH".
            when: The dates or range to search, e.g. "the first half of October".
        """
        return (
            f"I want to fly from {from_airport} {when}. Compare these "
            f"destinations on price: {destinations}.\n\n"
            "Use search_oneway_flights ONCE, passing the whole destination list "
            "and a date range -- the server compares them in a single call.\n\n"
            "Give me a table ranked by price: destination, cheapest fare, date, "
            "airline, stops. Then say which is the best value and why, using the "
            "low/typical/high verdict rather than price alone.\n\n"
            "State which destinations and dates were actually searched from "
            "search_coverage, and what the search cost from api_usage."
        )

    @mcp.prompt(
        name="plan_trip",
        title="Plan a round trip on a budget",
        description=(
            "Find round-trip options for a destination across flexible trip "
            "lengths, priced as paired legs, within a budget."
        ),
    )
    @document_params
    def plan_trip(
        from_airport: str,
        to_airport: str,
        when: str,
        nights: str = "5, 6 or 7",
        budget: str = "",
    ) -> str:
        """
        Args:
            from_airport: Origin IATA code, e.g. "TLV".
            to_airport: Destination IATA code, e.g. "FCO".
            when: Departure window, e.g. "sometime in May".
            nights: Trip lengths to compare, e.g. "5, 6 or 7".
            budget: Optional total budget, e.g. "$400".
        """
        cap = f" Keep the total under {budget}." if budget.strip() else ""
        return (
            f"Plan a round trip from {from_airport} to {to_airport} {when}, "
            f"staying {nights} nights.{cap}\n\n"
            "Use search_roundtrip_flights ONCE: pass departure_date_from and "
            "departure_date_to for the window, and a list for `nights` so trip "
            "lengths are compared in the same call. Do not search each length "
            "separately.\n\n"
            "Round trips here are priced as paired legs, so quote the total, not "
            "two one-way fares added together. For the best two or three options "
            "give me: total price, both dates, airline per leg, stops, and the "
            "buy_link. Say whether the total is low, typical or high for this "
            "route, and what the search cost from api_usage."
        )

    @mcp.prompt(
        name="is_this_fare_good",
        title="Is this fare a good deal?",
        description=(
            "Check a specific route and date against Google's historical price "
            "band and answer book-now or wait."
        ),
    )
    @document_params
    def is_this_fare_good(
        from_airport: str,
        to_airport: str,
        departure_date: str,
        quoted_price: str = "",
    ) -> str:
        """
        Args:
            from_airport: Origin IATA code, e.g. "TLV".
            to_airport: Destination IATA code, e.g. "BUD".
            departure_date: The date to check, "YYYY-MM-DD".
            quoted_price: Optional price you were quoted elsewhere, e.g. "$260".
        """
        compare = (
            f" I was quoted {quoted_price} elsewhere -- tell me if that is "
            "competitive."
            if quoted_price.strip()
            else ""
        )
        return (
            f"Check fares for {from_airport} to {to_airport} on "
            f"{departure_date}.{compare}\n\n"
            "Search that date, then answer the only question that matters: is "
            "this a good price right now, or should I wait?\n\n"
            "Base the verdict on price_range_in_relation_to_other_periods and "
            "the price_insights_low / price_insights_high band, not on the "
            "cheapest number you happen to see. Say explicitly where today's "
            "best fare sits inside that band.\n\n"
            "Do not reuse any earlier result -- fares move, so this must be a "
            "fresh lookup."
        )
