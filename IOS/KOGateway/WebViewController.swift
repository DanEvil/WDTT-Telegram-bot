import UIKit
import WebKit

/// K.O. Gateway — iOS/iPadOS-оболочка экосистемы (по мотивам Telegram mini-app).
///
/// В WKWebView открывается сайт K.O. Gateway: вход через Telegram, профиль
/// с подпиской (тариф, срок, устройства онлайн, трафик), ссылки подключения,
/// оплата, рефералы, админ-панель.
///
/// Ссылки wdtt:// и qwdtt:// открываются во внешнем клиенте, а если его нет —
/// копируются в буфер обмена. Ссылки t.me/... открываются в Telegram.
final class WebViewController: UIViewController, WKNavigationDelegate {

    private enum Constants {
        static let homeURL = URL(string: "https://auth.ko-gateway.ru/")!
        static let internalHosts: Set<String> = ["auth.ko-gateway.ru", "app.ko-gateway.ru"]
    }

    private var webView: WKWebView!

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        config.websiteDataStore = .default()   // cookies авторизации живут между запусками

        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .automatic
        webView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor)
        ])

        webView.load(URLRequest(url: Constants.homeURL))
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url,
              let scheme = url.scheme?.lowercased() else {
            decisionHandler(.allow)
            return
        }

        switch scheme {
        case "wdtt", "qwdtt":
            openSubscriptionLink(url)
            decisionHandler(.cancel)
        case "tg":
            openExternally(url)
            decisionHandler(.cancel)
        case "http", "https":
            let host = url.host?.lowercased() ?? ""
            if Constants.internalHosts.contains(host) {
                decisionHandler(.allow)
            } else if url.host?.lowercased().hasSuffix(".t.me") == true
                        || url.host?.lowercased() == "telegram.me" {
                openExternally(url)
                decisionHandler(.cancel)
            } else {
                decisionHandler(.allow)
            }
        default:
            decisionHandler(.cancel)
        }
    }

    /// Ссылка подключения: во внешний клиент WDTT, иначе — в буфер обмена.
    private func openSubscriptionLink(_ url: URL) {
        let app = UIApplication.shared
        if app.canOpenURL(url) {
            app.open(url, options: [:], completionHandler: nil)
        } else {
            UIPasteboard.general.string = url.absoluteString
            showAlert(title: "Ссылка скопирована",
                      message: "Вставьте её в приложение WDTT, чтобы подключиться.")
        }
    }

    private func openExternally(_ url: URL) {
        UIApplication.shared.open(url, options: [:], completionHandler: nil)
    }

    private func showAlert(title: String, message: String) {
        let alert = UIAlertController(title: title, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: true)
    }
}
