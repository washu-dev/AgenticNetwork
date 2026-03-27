#!/bin/bash
# Stop PiCrawler Thin Client
pkill -f "Multi_Spider_Agent_Thin_Client.thin_client" && echo "Stopped" || echo "Not running"
