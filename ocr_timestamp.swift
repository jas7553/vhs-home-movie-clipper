import Vision
import AppKit
import Foundation

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: ocr_timestamp <image_path> [<image_path> ...]\n", stderr)
    exit(1)
}

let paths = Array(CommandLine.arguments.dropFirst())

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["en-US"]

for path in paths {
    guard let image = NSImage(contentsOfFile: path),
          let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        fputs("Failed to load image: \(path)\n", stderr)
        print("\(path)\t")
        continue
    }

    let handler = VNImageRequestHandler(cgImage: cgImage)
    do {
        try handler.perform([request])
    } catch {
        fputs("OCR error for \(path): \(error)\n", stderr)
        print("\(path)\t")
        continue
    }

    var lines: [String] = []
    if let results = request.results {
        for obs in results {
            if let candidate = obs.topCandidates(1).first {
                lines.append(candidate.string)
            }
        }
    }
    // Join with space — parse_timestamp normalizes whitespace, so equivalent to \n
    print("\(path)\t\(lines.joined(separator: " "))")
}
