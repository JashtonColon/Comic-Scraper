package com.jashton.comicscraper

import android.os.Handler
import android.os.Looper
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebView
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * What the Python engine (android_bridge.py) uses to drive the app's built-in browser.
 * Python calls these from a background thread; the WebView work is done on the main thread.
 */
class WebBridge(
    private val web: WebView,
    private val ua: String,
    private val onVisible: (Boolean) -> Unit,
) {
    private val main = Handler(Looper.getMainLooper())
    private val results = ConcurrentHashMap<String, String>()
    private val latches = ConcurrentHashMap<String, CountDownLatch>()

    /** Called by the page's JavaScript with the answer to eval(). */
    @JavascriptInterface
    fun done(id: String, json: String) {
        results[id] = json
        latches[id]?.countDown()
    }

    fun userAgent(): String = ua

    fun cookies(url: String): String =
        try {
            CookieManager.getInstance().getCookie(url) ?: ""
        } catch (e: Exception) {
            ""
        }

    fun load(url: String) {
        main.post { web.loadUrl(url) }
    }

    fun setVisible(visible: Boolean) {
        main.post { onVisible(visible) }
    }

    /**
     * Run a JavaScript expression (it may return a Promise) and wait for its value.
     * Returns JSON: {"ok":true,"v":...}, {"ok":false,"e":"..."} or {"ok":false,"timeout":true}.
     */
    fun eval(expr: String, timeoutMs: Long): String {
        val id = UUID.randomUUID().toString().replace("-", "")
        val latch = CountDownLatch(1)
        latches[id] = latch
        val js = "(async()=>{try{const __v=await (" + expr + ");" +
            "ComicScraperBridge.done('" + id + "',JSON.stringify({ok:true,v:(__v===undefined?null:__v)}));}" +
            "catch(e){ComicScraperBridge.done('" + id + "',JSON.stringify({ok:false,e:String((e&&e.stack)||e)}));}" +
            "})();void 0;"
        main.post { web.evaluateJavascript(js, null) }
        val answered = try {
            latch.await(timeoutMs, TimeUnit.MILLISECONDS)
        } catch (e: InterruptedException) {
            false
        }
        latches.remove(id)
        val result = results.remove(id)
        return if (answered && result != null) result else "{\"ok\":false,\"timeout\":true}"
    }
}
