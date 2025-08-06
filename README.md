# Solana Sniper Jito Bot

This is a research sniper bot for Solana. It watches large token transfers on-chain via WebSocket (Triton or Helius), and reacts by sending buy transactions through Jupiter Aggregator API.

Then it submits the transaction using the Jito Bundle API for inclusion priority and MEV optimization.

## Features

- Watches token transfers in real-time
- Filters transfers larger than a given SOL threshold
- Automatically sends Jupiter swap tx
- Submits via Jito bundle for frontrunning

## Stack

- Python 3.10+
- Jupiter API
- Triton RPC / Helius (WebSocket)
- Jito Searcher Bundle API

## Status

Work in progress. Built for testing and research only.
