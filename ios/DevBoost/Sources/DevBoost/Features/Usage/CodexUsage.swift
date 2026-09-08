import Foundation
import SwiftUI
import UIKit

struct CodexUsageService: Sendable {
    let remote: any RemoteCommanding

    func refresh(on host: Host) async throws -> CodexUsageSnapshot {
        // The remote host owns the Codex login. Query ChatGPT upstream first;
        // the existing app-server query below remains the local fallback.
        let script = #"""
import json,os,subprocess,select,shutil,sys,time,urllib.request
try:
 home=os.path.expanduser(os.environ.get('CODEX_HOME','~/.codex'))
 with open(os.path.join(home,'auth.json'),encoding='utf-8') as f: auth=json.load(f)
 tokens=auth.get('tokens',auth)
 req=urllib.request.Request('https://chatgpt.com/backend-api/wham/usage',headers={'Authorization':'Bearer '+tokens['access_token'],'ChatGPT-Account-Id':tokens['account_id'],'User-Agent':'codex-cli','Accept':'application/json'})
 with urllib.request.urlopen(req,timeout=12) as response: payload=json.loads(response.read().decode('utf-8'))
 payload['devboostSource']='https'
 print(json.dumps(payload)); sys.exit(0)
except Exception:
 pass
codex=shutil.which('codex')
if not codex: print(json.dumps({'devboostError':'Codex is not installed or is not on the remote PATH.'})); sys.exit(0)
p=subprocess.Popen([codex,'app-server'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
def send(x): p.stdin.write(json.dumps(x)+'\n'); p.stdin.flush()
def request(i,method,params=None):
 send({'id':i,'method':method,'params':params})
 deadline=time.time()+12
 while time.time()<deadline:
  if not select.select([p.stdout],[],[],max(0,deadline-time.time()))[0]: raise Exception('Codex quota query timed out')
  line=p.stdout.readline()
  if not line:
   detail=(p.stderr.read() or '').strip()
   raise Exception(detail or f'codex app-server exited with status {p.poll()}')
  row=json.loads(line)
  if row.get('method') and 'id' in row: send({'id':row['id'],'error':{'code':-32601,'message':'DevBoost reads quotas only'}}); continue
  if row.get('id')==i:
   if 'error' in row: raise Exception('Codex could not read subscription quotas')
   return row['result']
 raise Exception('Codex quota query timed out')
try:
 request(1,'initialize',{'clientInfo':{'name':'devboost-ios','version':'1.0'}}); send({'method':'initialized'}); print(json.dumps(request(2,'account/rateLimits/read')))
except Exception as exc:
 print(json.dumps({'devboostError':str(exc)}))
finally:
 p.terminate()
 try: p.wait(timeout=2)
 except Exception: p.kill()
"""#
        let encoded = Data(script.utf8).base64EncodedString()
        let output = try await remote.execute("for codex_bin in \"$HOME\"/.nvm/versions/node/*/bin \"$HOME\"/.volta/bin \"$HOME\"/.asdf/shims \"$HOME\"/.npm-global/bin; do [ -d \"$codex_bin\" ] && PATH=\"$codex_bin:$PATH\"; done; export PATH=\"$HOME/.local/bin:$HOME/bin:/usr/local/bin:/opt/homebrew/bin:$PATH\"; printf %s \(shellQuote(encoded)) | base64 -d | python3", on: host)
        guard let data = output.data(using: String.Encoding.utf8) else { throw AppError.connectionFailed("Codex returned invalid quota data.") }
        do {
            guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw AppError.connectionFailed("Codex returned invalid quota data.") }
            if let message = object["devboostError"] as? String {
                throw AppError.connectionFailed("Codex usage query failed: \(message)")
            }
            return try CodexUsageParser.decode(object)
        } catch let error as AppError {
            throw error
        } catch {
            throw AppError.connectionFailed("Codex returned invalid quota data.")
        }
    }
}

enum CodexUsageParser {
    static func decode(_ object: [String: Any], now: Date = .now) throws -> CodexUsageSnapshot {
        if object["devboostSource"] as? String == "https" {
            return try decodeUpstream(object, now: now)
        }
        let buckets = (object["rateLimitsByLimitId"] as? [String: Any]) ?? ["codex": object["rateLimits"] as Any]
        guard let bucket = (buckets["codex"] as? [String: Any]) ?? buckets.values.compactMap({ $0 as? [String: Any] }).first else {
            throw AppError.connectionFailed("Codex returned no subscription quota data.")
        }
        let primary = bucket["primary"] as? [String: Any]
        let secondary = bucket["secondary"] as? [String: Any]
        let credits = bucket["credits"] as? [String: Any]
        let resetCredits = object["rateLimitResetCredits"] as? [String: Any]
        guard number(primary, "usedPercent") != nil || number(secondary, "usedPercent") != nil else {
            throw AppError.connectionFailed("Codex returned no subscription quota data.")
        }
        return CodexUsageSnapshot(
            primaryUsedPercent: number(primary, "usedPercent"), primaryResetAt: date(primary, "resetsAt"),
            secondaryUsedPercent: number(secondary, "usedPercent"), secondaryResetAt: date(secondary, "resetsAt"),
            availableResets: (resetCredits?["availableCount"] as? NSNumber)?.intValue,
            creditsUnlimited: credits?["unlimited"] as? Bool,
            creditBalance: credits?["balance"] as? String,
            planType: bucket["planType"] as? String,
            source: "[Local] Codex on remote host",
            updatedAt: now, message: nil
        )
    }

    private static func decodeUpstream(_ object: [String: Any], now: Date) throws -> CodexUsageSnapshot {
        guard let rateLimit = object["rate_limit"] as? [String: Any] else {
            throw AppError.connectionFailed("Codex returned no subscription quota data.")
        }
        let primary = rateLimit["primary_window"] as? [String: Any]
        let secondary = rateLimit["secondary_window"] as? [String: Any]
        guard number(primary, "used_percent") != nil || number(secondary, "used_percent") != nil else {
            throw AppError.connectionFailed("Codex returned no subscription quota data.")
        }
        let credits = object["credits"] as? [String: Any]
        let resetCredits = object["rate_limit_reset_credits"] as? [String: Any]
        return CodexUsageSnapshot(
            primaryUsedPercent: number(primary, "used_percent"), primaryResetAt: date(primary, "reset_at"),
            secondaryUsedPercent: number(secondary, "used_percent"), secondaryResetAt: date(secondary, "reset_at"),
            availableResets: (resetCredits?["available_count"] as? NSNumber)?.intValue,
            creditsUnlimited: credits?["unlimited"] as? Bool,
            creditBalance: credits?["balance"] as? String,
            planType: object["plan_type"] as? String,
            source: "[HTTPS] OpenAI upstream",
            updatedAt: now, message: nil
        )
    }

    private static func number(_ value: [String: Any]?, _ key: String) -> Double? {
        guard let value = value?[key] else { return nil }
        if let number = value as? Double { return number }
        if let number = value as? NSNumber { return number.doubleValue }
        return nil
    }
    private static func date(_ value: [String: Any]?, _ key: String) -> Date? {
        guard let seconds = number(value, key) else { return nil }
        return Date(timeIntervalSince1970: seconds > 10_000_000_000 ? seconds / 1_000 : seconds)
    }
}

struct UsageView: View {
    @EnvironmentObject private var store: AppStore
    @State private var hostID: UUID?
    @State private var refreshing = false
    private var host: Host? { store.hosts.first { $0.id == (hostID ?? store.hosts.first?.id) } }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("AI Usage & Balances").font(.title2.weight(.bold))
                        Text(host.map { "Codex on \($0.name)" } ?? "Select an SSH host")
                            .font(.caption).foregroundStyle(AppTheme.muted)
                        if let source = store.codexUsage.source {
                            Text(source).font(.caption2).foregroundStyle(AppTheme.muted)
                        }
                    }
                    Spacer()
                    Button("Refresh", systemImage: "arrow.clockwise") { Task { await refresh() } }
                        .buttonStyle(.bordered)
                        .tint(AppTheme.accent)
                        .disabled(host == nil || refreshing)
                }

                if !store.hosts.isEmpty {
                    Picker("Remote server", selection: $hostID) {
                        ForEach(store.hosts) { server in
                            Text(server.name).tag(Optional(server.id))
                        }
                    }
                    .pickerStyle(.menu)
                    .tint(AppTheme.accent)
                    .onChange(of: hostID) { _, _ in Task { await refresh() } }
                }

                if !LiveUsageActivity.areActivitiesEnabled {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("Live Activities are disabled. Enable them in Settings → DevBoost.", systemImage: "bell.slash")
                            .font(.footnote)
                            .foregroundStyle(.orange)
                        Button("Open DevBoost Settings", systemImage: "arrow.up.forward.app") {
                            guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
                            UIApplication.shared.open(url)
                        }
                        .font(.footnote.weight(.semibold))
                    }
                }

                if let resets = store.codexUsage.availableResets, resets > 0 {
                    Text("\(resets)X RESETS")
                        .font(.caption.weight(.bold))
                        .tracking(0.5)
                        .padding(.horizontal, 10).padding(.vertical, 5)
                        .background(Color(red: 0.125, green: 0.165, blue: 0.212), in: Capsule())
                        .overlay { Capsule().stroke(Color(red: 0.325, green: 0.384, blue: 0.459), lineWidth: 1) }
                }

                AppCard {
                    VStack(alignment: .leading, spacing: 14) {
                        Text("QUOTAS").font(.caption.weight(.bold)).tracking(1).foregroundStyle(AppTheme.muted)
                        UsageMeter(title: "Current window", used: store.codexUsage.primaryUsedPercent, reset: store.codexUsage.primaryResetAt)
                        UsageMeter(title: "Secondary window", used: store.codexUsage.secondaryUsedPercent, reset: store.codexUsage.secondaryResetAt)
                    }
                }

                AppCard {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("BALANCES").font(.caption.weight(.bold)).tracking(1).foregroundStyle(AppTheme.muted)
                        HStack {
                            Text("Credits").font(.headline)
                            Spacer()
                            Text(store.codexUsage.creditsUnlimited == true ? "Unlimited" : (store.codexUsage.creditBalance.map { "\($0) left" } ?? "—"))
                                .font(.headline.weight(.semibold)).foregroundStyle(AppTheme.accent)
                        }
                        if let plan = store.codexUsage.planType {
                            Text("Plan: \(plan.capitalized)").font(.caption).foregroundStyle(AppTheme.muted)
                        }
                    }
                }

                Text(store.codexUsage.message ?? store.codexUsage.updatedAt.map { "Updated \($0.formatted(date: .omitted, time: .shortened))" } ?? "Install Codex and sign in on an SSH host, then refresh.")
                    .font(.footnote).foregroundStyle(store.codexUsage.message == nil ? AppTheme.muted : .orange)
            }
            .padding()
        }
        .overlay { if store.hosts.isEmpty { ContentUnavailableView("No SSH hosts", systemImage: "chart.bar", description: Text("Codex usage is read securely from Codex on a saved SSH host.")) } }
        .navigationTitle("AI Usage")
        .onAppear { if hostID == nil { hostID = store.hosts.first?.id } }
        .task {
            while !Task.isCancelled {
                await refresh()
                try? await Task.sleep(for: .seconds(UsageRefreshPolicy.foregroundInterval))
            }
        }
    }
    private func refresh() async {
        guard let host, !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            store.codexUsage = try await CodexUsageService(remote: RemoteCommandService(keychain: store.keychain)).refresh(on: host)
            await LiveUsageActivity.sync(with: store.codexUsage, hostName: host.name)
        } catch {
            store.codexUsage = CodexUsageSnapshot(message: error.localizedDescription)
        }
    }
}

private struct UsageMeter: View {
    let title: String; let used: Double?; let reset: Date?
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).font(.subheadline.weight(.semibold))
                Spacer()
                if let reset { Text(resetDescription(reset)).font(.caption.monospaced()).foregroundStyle(AppTheme.muted) }
            }
            if let used {
                let remaining = max(0, min(100, 100 - used))
                ZStack {
                    GeometryReader { proxy in
                        RoundedRectangle(cornerRadius: 999).fill(Color(red: 0.05, green: 0.067, blue: 0.09))
                        RoundedRectangle(cornerRadius: 999).fill(usageColor(used)).frame(width: proxy.size.width * remaining / 100)
                    }
                    Text("\(Int(remaining.rounded()))% left")
                        .font(.caption.weight(.bold).monospaced())
                        .foregroundStyle(used <= 50 ? Color(red: 0.06, green: 0.15, blue: 0.1) : .white)
                        .shadow(color: .black.opacity(0.5), radius: 2)
                }
                .frame(height: 26)
                .clipShape(Capsule())
                .overlay { Capsule().stroke(Color.white.opacity(0.22), lineWidth: 1) }
            } else {
                Text("Not available").font(.caption).foregroundStyle(AppTheme.muted)
            }
        }
    }

    private func usageColor(_ used: Double) -> Color {
        if used <= 50 { return Color(red: 0.431, green: 0.906, blue: 0.627) }
        if used <= 75 { return Color(red: 0.918, green: 0.859, blue: 0.608) }
        if used <= 90 { return Color(red: 0.91, green: 0.71, blue: 0.557) }
        return Color(red: 0.906, green: 0.643, blue: 0.643)
    }

    private func resetDescription(_ date: Date) -> String {
        let minutes = max(0, Int(ceil(date.timeIntervalSinceNow / 60)))
        if minutes <= 0 { return "Resets now" }
        if minutes < 60 { return "Resets in \(minutes)m" }
        let hours = minutes / 60
        let remainingMinutes = minutes % 60
        if hours < 24 { return "Resets in \(hours)h\(remainingMinutes > 0 ? "\(remainingMinutes)m" : "")" }
        let days = hours / 24
        let remainingHours = hours % 24
        if days < 7 { return "Resets in \(days)d\(remainingHours > 0 ? "\(remainingHours)h" : "")" }
        let weeks = days / 7
        let remainingDays = days % 7
        return "Resets in \(weeks)w\(remainingDays > 0 ? "\(remainingDays)d" : "")"
    }
}
