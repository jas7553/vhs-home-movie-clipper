import Vision
import AppKit
import Foundation

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: ocr_timestamp <image_path> [<image_path> ...]\n", stderr)
    exit(1)
}

let paths = Array(CommandLine.arguments.dropFirst())

// One output line per path; index matches paths so stdout stays ordered.
var results = Array(repeating: "", count: paths.count)
let lock = NSLock()

let batchSize = 64

for batchStart in stride(from: 0, to: paths.count, by: batchSize) {
    let batchEnd = min(batchStart + batchSize, paths.count)
    let batchCount = batchEnd - batchStart

    DispatchQueue.concurrentPerform(iterations: batchCount) { j in
        let i = batchStart + j
        let path = paths[i]

        guard let image = NSImage(contentsOfFile: path),
              let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            lock.lock()
            fputs("Failed to load image: \(path)\n", stderr)
            lock.unlock()
            results[i] = "\(path)\t"
            return
        }

        // Each iteration needs its own request + handler — VNRecognizeTextRequest is not thread-safe
        // when shared across concurrent handlers.
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = .accurate
        req.usesLanguageCorrection = false
        req.recognitionLanguages = ["en-US"]

        let handler = VNImageRequestHandler(cgImage: cgImage)
        do {
            try handler.perform([req])
        } catch {
            lock.lock()
            fputs("OCR error for \(path): \(error)\n", stderr)
            lock.unlock()
            results[i] = "\(path)\t"
            return
        }

        var lines: [String] = []
        if let obs = req.results {
            for o in obs {
                if let candidate = o.topCandidates(1).first {
                    lines.append(candidate.string)
                }
            }
        }
        // Join with space — parse_timestamp normalizes whitespace, so equivalent to \n
        results[i] = "\(path)\t\(lines.joined(separator: " "))"
    }
}

for line in results {
    print(line)
}
