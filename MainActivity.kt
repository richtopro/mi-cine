package es.micine.app

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.text.InputType
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import org.json.JSONObject

/**
 * Pantalla principal: muestra la web de Mi Cine de tu servidor.
 * Cuando pulsas reproducir, la web llama a MiCineAndroid.play(...) y el vídeo
 * se abre en PlayerActivity (motor de VLC, que lo reproduce todo).
 */
class MainActivity : Activity() {

    companion object {
        const val DEFAULT_URL = "https://ricardus.frost.usbx.me/micine/"
        const val REQ_PLAY = 1
    }

    private lateinit var web: WebView
    private val prefs by lazy { getSharedPreferences("micine", MODE_PRIVATE) }
    private var errorShown = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        web.setBackgroundColor(Color.parseColor("#141414"))
        web.isFocusable = true
        web.isFocusableInTouchMode = true
        setContentView(web)

        val s = web.settings
        s.javaScriptEnabled = true
        s.domStorageEnabled = true
        s.databaseEnabled = true
        s.mediaPlaybackRequiresUserGesture = false
        s.loadWithOverviewMode = true
        s.useWideViewPort = true
        s.userAgentString = s.userAgentString + " MiCineApp/1"

        val cookies = CookieManager.getInstance()
        cookies.setAcceptCookie(true)
        cookies.setAcceptThirdPartyCookies(web, true)

        web.webChromeClient = WebChromeClient()
        web.webViewClient = object : WebViewClient() {
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) showError()
            }

            override fun onPageFinished(view: WebView, url: String) {
                errorShown = false
                CookieManager.getInstance().flush()
            }
        }
        web.addJavascriptInterface(Bridge(), "MiCineAndroid")

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState)
        } else {
            val url = prefs.getString("url", null)
            if (url == null) askUrl(DEFAULT_URL) else web.loadUrl(url)
        }
        web.requestFocus()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        web.saveState(outState)
    }

    override fun onPause() {
        super.onPause()
        CookieManager.getInstance().flush()
    }

    private fun askUrl(current: String) {
        val input = EditText(this)
        input.setText(current)
        input.setSingleLine()
        input.inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
        AlertDialog.Builder(this)
            .setTitle("Dirección de tu Mi Cine")
            .setView(input)
            .setCancelable(false)
            .setPositiveButton("Guardar") { _, _ ->
                var u = input.text.toString().trim()
                if (!u.startsWith("http")) u = "https://$u"
                if (!u.endsWith("/")) u += "/"
                prefs.edit().putString("url", u).apply()
                web.loadUrl(u)
            }
            .show()
    }

    private fun showError() {
        if (errorShown || isFinishing) return
        errorShown = true
        AlertDialog.Builder(this)
            .setTitle("No se puede conectar")
            .setMessage("Comprueba la conexión a internet o la dirección de tu servidor.")
            .setPositiveButton("Reintentar") { _, _ -> errorShown = false; web.reload() }
            .setNeutralButton("Cambiar dirección") { _, _ ->
                errorShown = false
                askUrl(prefs.getString("url", DEFAULT_URL) ?: DEFAULT_URL)
            }
            .show()
    }

    /** Funciones que la web puede llamar como window.MiCineAndroid.xxx() */
    inner class Bridge {
        @JavascriptInterface
        fun play(json: String) {
            runOnUiThread {
                try {
                    val o = JSONObject(json)
                    val i = Intent(this@MainActivity, PlayerActivity::class.java)
                    i.putExtra("url", o.getString("url"))
                    i.putExtra("title", o.optString("title"))
                    i.putExtra("subtitle", o.optString("subtitle"))
                    i.putExtra("start", o.optLong("start", 0L))
                    i.putExtra("fileId", o.optString("fileId"))
                    i.putExtra("key", o.optString("key"))
                    i.putExtra("hasNext", o.optBoolean("hasNext", false))
                    @Suppress("DEPRECATION")
                    startActivityForResult(i, REQ_PLAY)
                } catch (e: Exception) {
                    e.printStackTrace()
                }
            }
        }

        @JavascriptInterface
        fun changeServer() {
            runOnUiThread { askUrl(prefs.getString("url", DEFAULT_URL) ?: DEFAULT_URL) }
        }

        @JavascriptInterface
        fun exitApp() {
            runOnUiThread { finish() }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_PLAY || data == null) return
        val js = "window.miCineProgress && window.miCineProgress(" +
            JSONObject.quote(data.getStringExtra("fileId") ?: "") + "," +
            JSONObject.quote(data.getStringExtra("key") ?: "") + "," +
            data.getLongExtra("pos", 0L) + "," +
            data.getLongExtra("dur", 0L) + "," +
            data.getBooleanExtra("ended", false) + "," +
            data.getBooleanExtra("next", false) + ")"
        web.evaluateJavascript(js, null)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        // La web cierra primero lo que haya abierto (ficha, menú…); si no hay nada, salimos.
        web.evaluateJavascript("(window.miCineBack && window.miCineBack()) ? 'y' : 'n'") { v ->
            if (v == null || !v.contains("y")) finish()
        }
    }
}
