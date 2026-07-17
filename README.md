# LifeRoute — running it for real

A real Flask + SQLite web app: real hospital accounts, a real database, a
real geospatial matching algorithm, and a live Uber-style delivery pipeline.

## Run it

```
cd liferoute_app
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5050. First run seeds `liferoute.db` with 5 demo
hospitals (real Delhi coordinates) and randomized inventory across a full
catalog of blood products, equipment, specialist physicians, and pharmaceuticals.

Demo logins: `stxavier`, `northridge`, `cantt`, `rohini`, `gk` — all password `demo123`.
Click a demo row on the login screen to auto-fill it.

## Admin console

Open http://127.0.0.1:5050/admin/login (not linked anywhere in the public
site — bookmark it). Default credentials:

- Username: `admin`
- Password: `admin123`

Override these in production by setting the `LR_ADMIN_USERNAME` and
`LR_ADMIN_PASSWORD` environment variables before starting the app.

The console lists every registered hospital with its license number,
uploaded registration document (view or download), current subscription
plan, requests asked/fulfilled, live inventory, and a network reliability
score. It's split into four tabs:

- **Documentation** — license number, submitted document (view/download),
  submission date, 3-year renewal due date, verification status, and a
  one-click "Mark verified" toggle.
- **Inventory** — total stocked units and out-of-stock line counts per
  hospital.
- **Requests** — requests each hospital has asked for vs. fulfilled as a
  supplier.
- **Reliability** — each hospital's network reliability score (delivered ÷
  assigned supply requests), color-coded and sorted highest first.

All five demo hospitals (`stxavier`, `northridge`, `cantt`, `rohini`, `gk`)
come pre-seeded as verified with a sample license document already on file,
so the admin console has realistic data out of the box.

## Hospital documents page

Every hospital account has a **Documents** tab in its sidebar
(`/documents`) showing its license number, verification status, the
document it submitted, when it was submitted, and a "renew certification"
reminder that counts down to 3 years after submission (turning to a warning
inside the last 90 days, and an overdue notice after that).

## What's new in this pass

- **Full asset catalog**: 4 categories (Blood Products, Critical Equipment,
  Specialist Physicians, Pharmaceuticals & Antidotes) covering 30+ real asset
  types, plus a "Something Else" free-text option so any hospital can request
  or stock literally anything — it becomes a real row in the inventory table.
- **Live delivery pipeline**: requests move through
  `pending → matched → dispatched → in_transit → delivered`, each transition
  a real database write with a real timestamp, advanced by the fulfilling
  hospital from the request page.
- **Uber-style live tracking**: the request detail page polls
  `/api/request/<id>` every 3 seconds, animates the timeline as steps
  complete, counts down a real ETA, and moves a vehicle marker along the
  Leaflet map by interpolating between the two hospitals' real coordinates
  based on elapsed time vs. calculated ETA.
- **Live network map**: `/network` polls `/api/network` every 4 seconds and
  shows every in-flight shipment across the whole system moving in real time.
- **Redesigned UI**: sidebar navigation, Apple-style light theme (SF-style
  type stack, soft shadows, rounded cards), animated stat counters, inline
  quantity steppers that save via fetch as you click, category tabs for
  asset selection.

## What's actually real under the hood

- Passwords hashed with Werkzeug, real Flask sessions.
- `inventory` table drives every match — edit it live and watch the next
  request route differently.
- `new_request()` in `app.py` queries every other hospital's real inventory,
  computes true great-circle distance with the Haversine formula, sorts by
  distance, and locks (decrements) stock at the nearest hospital with enough
  supply. Tier only changes the assumed transport speed for ETA math.
- Everything persists in `liferoute.db` — restart the server, data's still there.

## For the pitch

Open the Network Map on the projector before you start. Log in as one
hospital in a second tab, submit a Tier 1 request, dispatch it, start
transit — the judges will watch the ambulance icon actually move across the
live map in the first tab in real time, computed from real ETA math, not a
canned animation.
