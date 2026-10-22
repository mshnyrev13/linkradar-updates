// Standard macOS Local Network check, without sending application data.
// The app remains the responsible code for this child (Apple TN3179).
import Foundation
import Network
import Darwin

guard CommandLine.arguments.count == 4,
      let index = Int(CommandLine.arguments[3]), index > 0 else { exit(64) }
let host = CommandLine.arguments[1]
let source = CommandLine.arguments[2]
let queue = DispatchQueue(label: "LinkRadar.network-access")
let monitor = NWPathMonitor()
var connection: NWConnection?
var denial = false
var finished = false

func finish(_ state: String, _ code: Int32) {
    guard !finished else { return }
    finished = true
    print(state)
    fflush(stdout)
    connection?.cancel()
    monitor.cancel()
    exit(code)
}

monitor.pathUpdateHandler = { path in
    guard connection == nil else { return }
    guard let selected = path.availableInterfaces.first(where: { $0.index == index }) else {
        finish("interface_unavailable", 4)
        return
    }
    let parameters = NWParameters.udp
    parameters.requiredInterface = selected
    parameters.requiredLocalEndpoint = .hostPort(host: NWEndpoint.Host(source), port: .any)
    let candidate = NWConnection(host: NWEndpoint.Host(host), port: 9, using: parameters)
    connection = candidate
    candidate.stateUpdateHandler = { state in
        switch state {
        case .ready:
            finish("ready", 0)
        case .waiting:
            denial = candidate.currentPath?.unsatisfiedReason == .localNetworkDenied
            // Keep the connection alive: the system can present its alert and
            // automatically retry when the user allows access. No send() call.
        case .failed:
            finish("network_unavailable", 5)
        default:
            break
        }
    }
    candidate.start(queue: queue)
}
queue.asyncAfter(deadline: .now() + 15) {
    finish(denial ? "local_network_denied" : "network_unavailable", denial ? 3 : 5)
}
monitor.start(queue: queue)
dispatchMain()
