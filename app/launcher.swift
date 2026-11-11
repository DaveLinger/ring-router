// Ring Router.app launcher.
//
// macOS privacy (Local Network, Calendars, Contacts, Automation, ...) is granted to the
// *responsible* app. A bare Homebrew python started by launchd has no app identity, so macOS
// silently denies Local Network access. This launcher is the app: it spawns the listener as a
// child (children inherit its identity) and stays alive as its parent, forwarding signals and
// exiting with the child's status so launchd's KeepAlive still works.
import Foundation

let bundleDir = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()  // .../Contents/MacOS
let configURL = bundleDir.deletingLastPathComponent().appendingPathComponent("Resources/command.txt")

// The command to run, one argument per line, e.g. /path/to/.venv/bin/ring-router / serve
var argv = (try? String(contentsOf: configURL, encoding: .utf8))?
    .split(separator: "\n").map { String($0).trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty } ?? []
if CommandLine.arguments.count > 1 { argv = Array(CommandLine.arguments.dropFirst()) }  // override for one-off runs
guard let exe = argv.first else {
    FileHandle.standardError.write("Ring Router: no command (Resources/command.txt)\n".data(using: .utf8)!)
    exit(64)
}

let child = Process()
child.executableURL = URL(fileURLWithPath: exe)
child.arguments = Array(argv.dropFirst())
child.environment = ProcessInfo.processInfo.environment

var _signalSources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { if child.isRunning { kill(child.processIdentifier, sig) } }
    src.resume()
    _signalSources.append(src)
}
child.terminationHandler = { p in exit(p.terminationStatus) }

do { try child.run() } catch {
    FileHandle.standardError.write("Ring Router: failed to start \(exe): \(error)\n".data(using: .utf8)!)
    exit(70)
}
dispatchMain()
