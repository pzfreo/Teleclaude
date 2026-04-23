# Feature Request: UK Train Times Integration

## Problem
Currently no way to check train times, delays, or platform information for UK rail travel.

## Proposed Solution
Integrate a UK train times API to enable queries like:
- Train times between stations
- Real-time delays and cancellations
- Platform information
- Live departure boards

## API Options

### Recommended: Huxley2
- Free, open source wrapper around National Rail Darwin
- Simple REST API
- https://huxley2.azurewebsites.net/
- No authentication required for basic usage
- Example: `https://huxley2.azurewebsites.net/departures/CHI/50`

### Alternatives
- **National Rail Darwin API** (official, requires registration)
- **realTimeTrains.co.uk API** (free for personal use)
- **TransportAPI** (paid)

## Implementation Approach

### Option 1: MCP Server (Recommended)
Create new MCP server for train data (similar to google-maps integration):
- `get_departures(station_code, num_trains)`
- `get_service_details(service_id)`
- `get_arrivals(station_code, num_trains)`
- `find_journey(from_station, to_station, time)`

### Option 2: Direct Integration
Add direct API integration using existing fetch tool

## Use Cases
- "When's the next train from London to Barnham?"
- "Is the 16:11 to Barnham on time?"
- "Check trains from Victoria to Chichester tomorrow morning"
- Add train arrival times to calendar automatically
- Set reminders to leave for station pickup

## Station Codes Reference
Common local stations:
- CHI - Chichester
- BAA - Barnham
- VIC - London Victoria
- WAT - London Waterloo
- CLJ - Clapham Junction

## Related Context
- User needed to pick up someone from Barnham station at 16:11
- Currently have to manually check Trainline/National Rail
- Integration with calendar reminders would be valuable

## Priority
Medium - useful for UK users, improves scheduling workflow

## Estimated Effort
- MCP Server approach: ~2-4 hours
- Testing with real stations: 1 hour
- Documentation: 30 mins
