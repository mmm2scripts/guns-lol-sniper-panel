import SwiftUI

@main
struct GunsSniperApp: App {
    var body: some Scene {
        WindowGroup {
            DashboardView()
                .ignoresSafeArea()
                .background(Color(red: 8 / 255, green: 8 / 255, blue: 12 / 255).ignoresSafeArea())
                .preferredColorScheme(.dark)
        }
    }
}
