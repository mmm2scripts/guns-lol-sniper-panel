import SwiftUI
import WebKit

/// Hosts the bundled dashboard (../web). The dashboard talks to your server over HTTPS with
/// `Authorization: Bearer <API key>`. The server URL and key are typed in by the user at runtime and
/// stored in the iOS Keychain through the `secureStore` bridge below - nothing is hard-coded in the app.
struct DashboardView: UIViewRepresentable {
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.userContentController.addScriptMessageHandler(context.coordinator, contentWorld: .page, name: "secureStore")

        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = context.coordinator
        web.uiDelegate = context.coordinator
        let bg = UIColor(red: 8 / 255, green: 8 / 255, blue: 12 / 255, alpha: 1)
        web.isOpaque = false
        web.backgroundColor = bg
        web.scrollView.backgroundColor = bg
        web.scrollView.contentInsetAdjustmentBehavior = .never   // the page handles safe areas itself
        web.scrollView.bounces = false
        web.allowsLinkPreview = false

        if let index = Bundle.main.url(forResource: "index", withExtension: "html", subdirectory: "web") {
            web.loadFileURL(index, allowingReadAccessTo: index.deletingLastPathComponent())
        }
        return web
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKScriptMessageHandlerWithReply, WKNavigationDelegate, WKUIDelegate {
        private let allowedKeys: Set<String> = ["url", "key"]

        // JS -> Keychain bridge (get / set / delete). Only the bundled local page may call it.
        func userContentController(_ userContentController: WKUserContentController,
                                   didReceive message: WKScriptMessage,
                                   replyHandler: @escaping (Any?, String?) -> Void) {
            guard message.frameInfo.isMainFrame,
                  message.frameInfo.request.url?.isFileURL == true,
                  let body = message.body as? [String: Any],
                  let op = body["op"] as? String,
                  let key = body["key"] as? String,
                  allowedKeys.contains(key) else {
                replyHandler(nil, "rejected")
                return
            }
            switch op {
            case "get":
                replyHandler(Keychain.get(key), nil)
            case "set":
                if let value = body["value"] as? String { replyHandler(Keychain.set(value, for: key), nil) }
                else { replyHandler(false, nil) }
            case "delete":
                Keychain.delete(key)
                replyHandler(true, nil)
            default:
                replyHandler(nil, "unknown op")
            }
        }

        // Only the bundled page may load in the web view; web links open in Safari.
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
            if url.isFileURL { decisionHandler(.allow); return }
            if navigationAction.navigationType == .linkActivated, url.scheme == "https" {
                UIApplication.shared.open(url)
            }
            decisionHandler(.cancel)
        }

        // target="_blank" links (e.g. https://guns.lol/<name>) -> Safari
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = navigationAction.request.url, url.scheme == "https" { UIApplication.shared.open(url) }
            return nil
        }
    }
}
