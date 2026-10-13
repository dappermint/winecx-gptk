import Foundation
import AppKit
import ApplicationServices
import CoreGraphics
import Vision
import CoreML
import Darwin
import ScreenCaptureKit

let args = CommandLine.arguments
func desktopInteractive() -> Bool {
    guard let app = NSWorkspace.shared.frontmostApplication else { return false }
    return app.bundleIdentifier != "com.apple.loginwindow"
}
func emit(_ value: Any) throws {
    print(String(data: try JSONSerialization.data(withJSONObject: value), encoding: .utf8)!)
}
if args[1] == "preflight" {
    try emit(["accessibility": AXIsProcessTrusted(), "screen_recording": CGPreflightScreenCaptureAccess(),
              "desktop_interactive": desktopInteractive()])
    exit(0)
}
if args[1] == "request-permissions" {
    let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    let accessibility = AXIsProcessTrustedWithOptions(options)
    let screenRecording = CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess()
    try emit(["accessibility": accessibility, "screen_recording": screenRecording])
    exit(0)
}
if args[1] == "ocr" {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .fast
    for (stage, devices) in try request.supportedComputeStageDevices {
        guard let cpu = devices.first(where: { if case .cpu = $0 { return true }; return false }) else { exit(2) }
        request.setComputeDevice(cpu, for: stage)
    }
    request.recognitionLanguages = ["en-US"]
    try VNImageRequestHandler(url: URL(fileURLWithPath: args[2])).perform([request])
    try emit((request.results ?? []).compactMap { $0.topCandidates(1).first?.string })
    exit(0)
}
let pid = pid_t(args[2])!
func processIdentity(_ target: pid_t) -> String? {
    var info = proc_bsdinfo()
    let size = Int32(MemoryLayout<proc_bsdinfo>.size)
    if proc_pidinfo(target, PROC_PIDTBSDINFO, 0, &info, size) == size,
       info.pbi_start_tvsec > 0 {
        return "kernel:\(info.pbi_start_tvsec):\(info.pbi_start_tvusec)"
    }
    return NSRunningApplication(processIdentifier: target)?.launchDate.map {
        "app:\($0.timeIntervalSince1970)"
    }
}
if args[1] == "process-identity" {
    try emit(["pid": Int(pid), "identity": processIdentity(pid) as Any? ?? NSNull(),
              "app_launch_date_available": NSRunningApplication(processIdentifier: pid)?.launchDate != nil])
    exit(0)
}
func status(_ reason: String? = nil) -> [String: Any] {
    let frontmost = NSWorkspace.shared.frontmostApplication
    let target = NSRunningApplication(processIdentifier: pid)
    let windows = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]] ?? []
    let targetWindows = windows.filter {
        ($0[kCGWindowOwnerPID as String] as? Int) == Int(pid) &&
        (($0[kCGWindowBounds as String] as? [String: Double])?["Width"] ?? 0) > 200
    }
    let failure = reason ?? (target == nil ? "target_not_running" :
        frontmost?.processIdentifier != pid ? "not_foreground" :
        targetWindows.isEmpty ? "target_window_missing" : "none")
    return ["failure_reason": failure, "target_pid": Int(pid),
            "target_running": target != nil,
            "frontmost_pid": frontmost.map { Int($0.processIdentifier) } as Any? ?? NSNull(),
            "frontmost_app": frontmost?.localizedName as Any? ?? NSNull(),
            "frontmost_bundle_id": frontmost?.bundleIdentifier as Any? ?? NSNull(),
            "desktop_interactive": desktopInteractive(),
            "target_window_present": !targetWindows.isEmpty,
            "target_window_count": targetWindows.count]
}
if args[1] == "status" {
    try emit(status())
    exit(0)
}
if args[1] == "activate" {
    guard desktopInteractive(), let identity = processIdentity(pid),
          let app = NSRunningApplication(processIdentifier: pid) else {
        try emit(status("activation_target_unavailable"))
        exit(2)
    }
    let requested = app.activate(options: [])
    let deadline = ProcessInfo.processInfo.systemUptime + 4
    var raised = false
    var accessibilityResult: Int32? = nil
    if NSWorkspace.shared.frontmostApplication?.processIdentifier != pid && AXIsProcessTrusted() {
        let accessibleApp = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(accessibleApp, 0.5)
        var value: CFTypeRef?
        if AXUIElementCopyAttributeValue(accessibleApp, kAXWindowsAttribute as CFString, &value) == .success,
           let windows = value as? [AXUIElement], windows.count == 1 {
            var windowPID: pid_t = 0
            if AXUIElementGetPid(windows[0], &windowPID) == .success && windowPID == pid,
               processIdentity(pid) == identity {
                let front = AXUIElementSetAttributeValue(accessibleApp, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
                let result = AXUIElementPerformAction(windows[0], kAXRaiseAction as CFString)
                raised = result == .success
                accessibilityResult = (result == .success ? front : result).rawValue
            }
        }
    }
    while ProcessInfo.processInfo.systemUptime < deadline && processIdentity(pid) == identity && desktopInteractive() {
        if NSWorkspace.shared.frontmostApplication?.processIdentifier == pid {
            var result = status("none")
            result["activation_requested"] = requested
            result["window_raised"] = raised
            try emit(result)
            exit(0)
        }
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.05))
    }
    var failure = status("foreground_acquisition_failed")
    failure["activation_requested"] = requested
    failure["window_raised"] = raised
    failure["accessibility_result"] = accessibilityResult as Any? ?? NSNull()
    try emit(failure)
    exit(2)
}
if args[1] == "quit" {
    guard let app = NSRunningApplication(processIdentifier: pid), app.terminate() else { exit(2) }
    exit(0)
}
if args[1] == "capture-window" {
    guard desktopInteractive() else {
        try emit(status("desktop_not_interactive"))
        exit(2)
    }
    func targetWindows() -> [[String: Any]] {
        let windows = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]] ?? []
        return windows.filter {
            let bounds = $0[kCGWindowBounds as String] as? [String: Double] ?? [:]
            return ($0[kCGWindowOwnerPID as String] as? Int) == Int(pid) &&
                ($0[kCGWindowLayer as String] as? Int) == 0 &&
                ($0[kCGWindowIsOnscreen as String] as? Bool) == true &&
                ($0[kCGWindowAlpha as String] as? Double ?? 0) > 0 &&
                (bounds["Width"] ?? 0) > 200 && (bounds["Height"] ?? 0) > 100
        }
    }
    guard let launched = processIdentity(pid) else {
        try emit(status("target_identity_unavailable"))
        exit(2)
    }
    let candidates = targetWindows()
    guard candidates.count == 1,
          let windowID = candidates.first?[kCGWindowNumber as String] as? UInt32 else {
        try emit(status(candidates.isEmpty ? "target_window_missing_or_minimized" : "target_window_ambiguous"))
        exit(2)
    }
    final class CaptureResult: @unchecked Sendable {
        let ready = DispatchSemaphore(value: 0)
        var image: CGImage?
        var failure: String?
        func finish(_ image: CGImage?, _ failure: String?) {
            self.image = image
            self.failure = failure
            ready.signal()
        }
    }
    let captured = CaptureResult()
    SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: true) { content, error in
        guard let window = content?.windows.first(where: {
            $0.windowID == windowID && $0.owningApplication?.processID == pid && $0.isOnScreen
        }) else {
            captured.finish(nil, error?.localizedDescription ?? "target_window_not_shareable")
            return
        }
        let filter = SCContentFilter(desktopIndependentWindow: window)
        let config = SCStreamConfiguration()
        config.width = max(1, Int(filter.contentRect.width * CGFloat(filter.pointPixelScale)))
        config.height = max(1, Int(filter.contentRect.height * CGFloat(filter.pointPixelScale)))
        config.showsCursor = false
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) { image, error in
            captured.finish(image, error?.localizedDescription)
        }
    }
    let deadline = ProcessInfo.processInfo.systemUptime + 12
    var completed = false
    while ProcessInfo.processInfo.systemUptime < deadline {
        if captured.ready.wait(timeout: .now()) == .success {
            completed = true
            break
        }
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.01))
    }
    let after = targetWindows()
    guard completed, let image = captured.image, desktopInteractive(),
          processIdentity(pid) == launched,
          after.count == 1,
          (after.first?[kCGWindowNumber as String] as? UInt32) == windowID else {
        try? FileManager.default.removeItem(atPath: args[3])
        var failure = status(!desktopInteractive() ? "desktop_not_interactive" :
            !completed ? "screen_capture_timeout" :
            captured.image == nil ? "screen_capture_failed" : "window_identity_changed_during_capture")
        if completed { failure["capture_error"] = captured.failure as Any? ?? NSNull() }
        failure["window_id"] = windowID
        try emit(failure)
        exit(2)
    }
    guard let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else {
        try emit(status("png_encoding_failed"))
        exit(2)
    }
    try png.write(to: URL(fileURLWithPath: args[3]), options: .atomic)
    var result = status("none")
    result["window_id"] = windowID
    result["capture_backend"] = "ScreenCaptureKit"
    try emit(result)
    exit(0)
}
guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else {
    try emit(status("not_foreground"))
    exit(2)
}
let windows = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]] ?? []
guard let window = windows.first(where: {
    ($0[kCGWindowOwnerPID as String] as? Int) == Int(pid) &&
    (($0[kCGWindowBounds as String] as? [String: Double])?["Width"] ?? 0) > 200
}), let bounds = window[kCGWindowBounds as String] as? [String: Double] else {
    try emit(status("target_window_missing"))
    exit(2)
}
let rect = CGRect(x: bounds["X"]!, y: bounds["Y"]!, width: bounds["Width"]!, height: bounds["Height"]!)
if args[1] == "capture" {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    proc.arguments = ["-x", "-R\(Int(rect.minX)),\(Int(rect.minY)),\(Int(rect.width)),\(Int(rect.height))", args[3]]
    try proc.run()
    proc.waitUntilExit()
    guard proc.terminationStatus == 0,
          NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else {
        try? FileManager.default.removeItem(atPath: args[3])
        var failure = status(proc.terminationStatus != 0 ? "screencapture_failed" : "foreground_changed_during_capture")
        failure["screencapture_returncode"] = Int(proc.terminationStatus)
        try emit(failure)
        exit(2)
    }
} else if args[1] == "key" {
    let key = CGKeyCode(args[3])!
    guard let launched = processIdentity(pid) else { exit(2) }
    CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: true)?.postToPid(pid)
    Thread.sleep(forTimeInterval: args.count > 4 ? Double(args[4])! : 0.1)
    guard processIdentity(pid) == launched else {
        try emit(status("target_identity_changed_during_input"))
        exit(2)
    }
    CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: false)?.postToPid(pid)
} else if args[1] == "move" {
    let point = CGPoint(x: rect.minX + Double(args[3])! * rect.width,
                        y: rect.minY + Double(args[4])! * rect.height)
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
} else { exit(1) }
