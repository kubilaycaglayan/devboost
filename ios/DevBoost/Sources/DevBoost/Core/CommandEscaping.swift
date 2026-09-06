import Foundation

/// Quotes one argument for the POSIX shell used by a remote SSH command.
func shellQuote(_ value: String) -> String {
    "'" + value.replacingOccurrences(of: "'", with: "'\"'\"'") + "'"
}
