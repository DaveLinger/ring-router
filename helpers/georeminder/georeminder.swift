// georeminder: create an Apple Reminder that fires when you arrive at / leave a place.
//
//   georeminder --title "Take out the trash" --address "1 Main St, Town, ST 12345"
//               [--proximity enter|leave] [--radius 100] [--place Home] [--list "Reminders"]
//   georeminder --title ... --lat 39.48 --lon -80.14 ...
//
// Geocodes the address with Apple's geocoder (no location permission needed), then saves an
// EKReminder with a location alarm. Reminders syncs it via iCloud, so the geofence fires on the
// iPhone. Prints one JSON line: {"ok":true,"id":...,"lat":...,"lon":...} or {"ok":false,"error":...}.
import CoreLocation
import EventKit
import Foundation

func emit(_ obj: [String: Any]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: obj)
    print(String(data: data, encoding: .utf8)!)
    exit((obj["ok"] as? Bool) == true ? 0 : 1)
}

var args: [String: String] = [:]
var it = CommandLine.arguments.dropFirst().makeIterator()
while let k = it.next() {
    guard k.hasPrefix("--"), let v = it.next() else { emit(["ok": false, "error": "bad argument \(k)"]) }
    args[String(k.dropFirst(2))] = v
}
guard let title = args["title"], !title.isEmpty else { emit(["ok": false, "error": "--title required"]) }
let proximity: EKAlarmProximity = (args["proximity"] ?? "enter") == "leave" ? .leave : .enter
let radius = Double(args["radius"] ?? "100") ?? 100
let placeName = args["place"] ?? "Home"

// Wait for an async callback while keeping the main run loop alive (CLGeocoder needs it).
func wait(_ done: () -> Bool, timeout: TimeInterval = 30) -> Bool {
    let end = Date().addingTimeInterval(timeout)
    while !done() && Date() < end { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
    return done()
}

// 1. Coordinates
var coord: CLLocationCoordinate2D?
if let lat = args["lat"].flatMap(Double.init), let lon = args["lon"].flatMap(Double.init) {
    coord = CLLocationCoordinate2D(latitude: lat, longitude: lon)
} else if let address = args["address"] {
    var finished = false
    var geoError: String?
    CLGeocoder().geocodeAddressString(address) { marks, err in
        if let c = marks?.first?.location?.coordinate { coord = c } else { geoError = err?.localizedDescription ?? "no match" }
        finished = true
    }
    if !wait({ finished }) { emit(["ok": false, "error": "geocoding timed out"]) }
    if coord == nil { emit(["ok": false, "error": "could not geocode address: \(geoError ?? "unknown")"]) }
} else {
    emit(["ok": false, "error": "--address or --lat/--lon required"])
}

// 2. Reminders access
let store = EKEventStore()
var granted = false, asked = false
store.requestFullAccessToReminders { ok, _ in granted = ok; asked = true }
if !wait({ asked }) || !granted { emit(["ok": false, "error": "Reminders full access not granted"]) }

// 3. Reminder with a location alarm
let reminder = EKReminder(eventStore: store)
reminder.title = title
if let listName = args["list"] {
    guard let cal = store.calendars(for: .reminder).first(where: { $0.title == listName }) else {
        emit(["ok": false, "error": "no Reminders list named \(listName)"])
    }
    reminder.calendar = cal
} else {
    reminder.calendar = store.defaultCalendarForNewReminders()
}
let place = EKStructuredLocation(title: placeName)
place.geoLocation = CLLocation(latitude: coord!.latitude, longitude: coord!.longitude)
place.radius = radius
let alarm = EKAlarm()
alarm.structuredLocation = place
alarm.proximity = proximity
reminder.addAlarm(alarm)
do { try store.save(reminder, commit: true) } catch { emit(["ok": false, "error": "save failed: \(error.localizedDescription)"]) }
emit(["ok": true, "id": reminder.calendarItemIdentifier, "lat": coord!.latitude, "lon": coord!.longitude,
      "list": reminder.calendar.title, "proximity": proximity == .leave ? "leave" : "enter", "radius": radius])
