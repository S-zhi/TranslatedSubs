import Foundation

public enum AppIdentity {
    public static func displayName(info: [String: Any]) -> String {
        for key in ["CFBundleDisplayName", "CFBundleName"] {
            if let value = info[key] as? String,
               !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return value
            }
        }
        return "TranslateSub"
    }
}
