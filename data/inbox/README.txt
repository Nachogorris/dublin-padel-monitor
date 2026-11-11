Dublin Padel Monitor — Multi-Snapshot Zero-API Browser Dropzone
===============================================================

Track next week's Peak & Off-Peak bookings across multiple snapshot passes
(7d, 3d, 24h, morning/6h, prepeak/1h) without any API key:

1. Scaffold a ready-to-fill 5-pass weekly pack:
       python monitor.py init-week-pack --week-start 2026-11-09

2. Supported filenames in data/inbox/ or data/inbox/week_YYYY-MM-DD/:
   - Single-day snapshot : <club_id>__YYYY-MM-DD__<tag>.txt (or .html)
     Examples: house_of_padel__2026-11-10__7d.txt
               house_of_padel__2026-11-10__morning.txt
               house_of_padel__2026-11-10__prepeak.html
   - 7-day weekly bundle : <club_id>__week__YYYY-MM-DD__<tag>.txt
     Tags: 7d | 3d | 24h | morning (or 6h) | prepeak (or 1h) | YYYYMMDDTHHMM

3. Three zero-API input formats accepted inside any file:
   (a) Rapid shorthand : 09:00-17:00 = 3 free €30
                         17:00-21:30 = 0 free €45
   (b) Copy-pasted text: Court 1 / 09:00 60 min €30 / Court 2 / No slots available
   (c) Webpage HTML    : Ctrl+S ('Webpage, HTML Only')

4. Ingest & build the color-coded Club x Hour (06:00-23:00) Peak/Off-Peak Heatmap:
       python monitor.py analyze-week --week-start 2026-11-09
