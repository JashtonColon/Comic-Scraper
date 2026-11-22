package com.jashton.comicscraper

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.Editable
import android.text.InputType
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/** Python calls this when something changed: "table", "candidates" or "done". */
class UiEvents(private val activity: MainActivity) {
    fun event(name: String) {
        activity.onPyEvent(name)
    }
}

/** Python's print() output goes here and into the Progress box. */
class LogSink(private val activity: MainActivity) {
    fun write(text: String) {
        activity.appendLog(text)
    }
}

class MainActivity : Activity() {
    private val main = Handler(Looper.getMainLooper())
    private val py = Executors.newSingleThreadExecutor()      // every call into Python goes through here
    @Volatile private var engine: PyObject? = null
    private lateinit var prefs: SharedPreferences

    private lateinit var web: WebView
    private lateinit var webBridge: WebBridge
    private lateinit var panel: LinearLayout
    private lateinit var browserBar: LinearLayout
    private var browserShown = false

    private lateinit var logView: TextView
    private lateinit var logScroll: ScrollView
    private val logBuf = StringBuilder()
    private val logPending = AtomicBoolean(false)

    private var busy = false
    private var hasPlan = false

    // tabs
    private val tabViews = mutableListOf<View>()
    private val tabButtons = mutableListOf<Button>()

    // single series
    private lateinit var linkEdit: EditText
    private lateinit var linkCheck: TextView
    private lateinit var startEdit: EditText
    private lateinit var endEdit: EditText
    private lateinit var allBox: CheckBox
    private lateinit var nameEdit: EditText
    private lateinit var groupEdit: EditText
    private lateinit var prefixEdit: EditText
    private lateinit var startBtn: Button

    // reading list
    private lateinit var rlUrl: EditText
    private lateinit var rlPasteBox: CheckBox
    private lateinit var rlText: EditText
    private lateinit var loadBtn: Button
    private lateinit var findBtn: Button
    private lateinit var dlBtn: Button
    private lateinit var savedBtn: Button
    private lateinit var rlTitle: TextView
    private lateinit var rlSummary: TextView
    private lateinit var rlList: ListView
    private var rows = JSONArray()
    private val rowAdapter = RowAdapter()

    // settings
    private lateinit var rootEdit: EditText
    private lateinit var perFolderEdit: EditText
    private lateinit var verboseBox: CheckBox
    private lateinit var accessText: TextView

    private lateinit var stopBtn: Button
    private var matchDialog: MatchDialog? = null

    // =================================================================== start-up
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences("settings", Context.MODE_PRIVATE)
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        web = WebView(this)
        setupWeb()
        buildUi()
        showTab(prefs.getInt("tab", 0))
        appendLog("Starting...\n")
        val ui = UiEvents(this)
        val log = LogSink(this)
        py.execute {
            try {
                val mod = Python.getInstance().getModule("android_bridge")
                mod.callAttr("init", webBridge, ui, log, filesDir.absolutePath)
                engine = mod
                appendLog("Ready.\n")
                val last = prefs.getString("rl_last", "") ?: ""
                if (last.isNotEmpty()) mod.callAttr("rl_open", last)
            } catch (e: Exception) {
                appendLog("Couldn't start the engine: ${e.message}\n")
            }
            main.post { refreshTable() }
        }
    }

    private fun setupWeb() {
        val s = web.settings
        s.javaScriptEnabled = true
        s.domStorageEnabled = true
        s.loadWithOverviewMode = true
        s.useWideViewPort = true
        s.mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
        // look like normal mobile Chrome (some sites treat in-app browsers differently)
        val ua = s.userAgentString.replace("; wv", "").replace(Regex(" Version/[0-9.]+"), "")
        s.userAgentString = ua
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true)
        web.webViewClient = WebViewClient()                 // keep every page inside the app
        if (Build.VERSION.SDK_INT >= 26) {
            web.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_IMPORTANT, false)
        }
        webBridge = WebBridge(web, ua) { show -> showBrowser(show) }
        web.addJavascriptInterface(webBridge, "ComicScraperBridge")
        web.loadUrl("about:blank")
    }

    // =================================================================== layout helpers
    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun label(text: String, size: Float = 14f, bold: Boolean = false, color: Int = Color.DKGRAY): TextView =
        TextView(this).apply {
            this.text = text
            textSize = size
            setTextColor(color)
            if (bold) setTypeface(typeface, Typeface.BOLD)
            setPadding(0, dp(6), 0, dp(2))
        }

    private fun edit(hint: String, value: String, number: Boolean = false, lines: Int = 1): EditText =
        EditText(this).apply {
            this.hint = hint
            setText(value)
            textSize = 15f
            if (number) {
                inputType = InputType.TYPE_CLASS_NUMBER
            } else if (lines > 1) {
                inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
                minLines = 3
                maxLines = lines
                gravity = Gravity.TOP
            } else {
                inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
                setSingleLine(true)
            }
        }

    private fun button(text: String, onClick: () -> Unit): Button =
        Button(this).apply {
            this.text = text
            isAllCaps = false
            setOnClickListener { onClick() }
        }

    private fun row(vararg views: View, weights: Boolean = true): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            for (v in views) {
                if (weights) addView(v, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
                else addView(v)
            }
        }

    private fun column(): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(4), dp(12), dp(8))
        }

    private fun pref(key: String, default: String = ""): String = prefs.getString(key, default) ?: default

    // =================================================================== the screen
    private fun buildUi() {
        val root = FrameLayout(this)
        root.addView(web, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.WHITE)
        }
        root.addView(panel, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        // ---- title + tabs
        val version = try { packageManager.getPackageInfo(packageName, 0).versionName } catch (e: Exception) { "" }
        val header = column()
        header.addView(label("Comic Scraper  v$version", 18f, true, Color.BLACK))
        val tabs = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        listOf("Single series", "Reading list", "Settings").forEachIndexed { i, name ->
            val b = button(name) { showTab(i) }
            tabButtons.add(b)
            tabs.addView(b, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        }
        header.addView(tabs)
        panel.addView(header)

        // ---- tab pages
        val pages = FrameLayout(this)
        tabViews.add(buildSingle())
        tabViews.add(buildReadingList())
        tabViews.add(buildSettings())
        for (v in tabViews) pages.addView(v, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        panel.addView(pages, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 3f))

        // ---- shared controls + progress log
        stopBtn = button("Stop") { stopJob() }
        stopBtn.isEnabled = false
        val showDetails = CheckBox(this).apply {
            text = "Show details"
            isChecked = prefs.getBoolean("verbose", false)
            setOnCheckedChangeListener { _, on ->
                prefs.edit().putBoolean("verbose", on).apply()
                if (::verboseBox.isInitialized && verboseBox.isChecked != on) verboseBox.isChecked = on
                call("set_verbose", on)
            }
        }
        val controls = row(stopBtn, button("Show browser") { showBrowser(true) }, showDetails)
        controls.setPadding(dp(8), 0, dp(8), 0)
        panel.addView(controls)
        logView = TextView(this).apply {
            typeface = Typeface.MONOSPACE
            textSize = 11f
            setTextColor(Color.rgb(220, 220, 220))
            setPadding(dp(8), dp(6), dp(8), dp(6))
            setTextIsSelectable(true)
        }
        logScroll = ScrollView(this).apply {
            setBackgroundColor(Color.rgb(30, 30, 30))
            addView(logView)
        }
        panel.addView(logScroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 2f))

        // ---- bar shown over the browser
        browserBar = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.argb(235, 33, 33, 33))
            setPadding(dp(12), dp(8), dp(12), dp(8))
            visibility = View.GONE
        }
        browserBar.addView(label("If you see a \"verify you are human\" check, tap it. The app goes back by itself when it's done.",
            13f, false, Color.WHITE))
        browserBar.addView(button("Back to the app") { showBrowser(false) })
        root.addView(browserBar, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM))

        setContentView(root)
    }

    private fun buildSingle(): View {
        val c = column()
        c.addView(label("Series page or first issue (Batcave, MangaDex or WeebCentral)"))
        linkEdit = edit("https://batcave.biz/...", pref("start_url"))
        c.addView(linkEdit)
        linkCheck = label("", 12f)
        c.addView(linkCheck)
        val checkLater = Runnable { checkLink() }
        linkEdit.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
            override fun afterTextChanged(s: Editable?) {
                main.removeCallbacks(checkLater)
                main.postDelayed(checkLater, 400)
            }
        })

        startEdit = edit("1", pref("start_issue", "1"), number = true)
        endEdit = edit("1", pref("end_issue", "1"), number = true)
        c.addView(row(label("Issues"), startEdit, label("  to"), endEdit))
        allBox = CheckBox(this).apply {
            text = "Get every issue (ignores the numbers)"
            isChecked = prefs.getBoolean("all_issues", false)
            setOnCheckedChangeListener { _, on ->
                startEdit.isEnabled = !on
                endEdit.isEnabled = !on
            }
        }
        startEdit.isEnabled = !allBox.isChecked
        endEdit.isEnabled = !allBox.isChecked
        c.addView(allBox)

        c.addView(label("Comic name (its folder)"))
        nameEdit = edit("Long Halloween", pref("folder_name"))
        c.addView(nameEdit)
        c.addView(label("Group folder (optional)"))
        groupEdit = edit("Batman", pref("sub_path"))
        c.addView(groupEdit)
        c.addView(label("File name start (optional)"))
        prefixEdit = edit("LongHalloween", pref("filename_prefix"))
        c.addView(prefixEdit)

        startBtn = button("Start") { startJob("run_single", settingsJson()) }
        c.addView(startBtn)
        return ScrollView(this).apply { addView(c) }
    }

    private fun buildReadingList(): View {
        val c = column()
        c.addView(label("League of Comic Geeks list link"))
        rlUrl = edit("https://leagueofcomicgeeks.com/profile/.../lists/...", pref("rl_url"))
        c.addView(rlUrl)
        rlText = edit("Superman #1-176, #0, Annual #1-12", pref("rl_text"), lines = 6)
        rlPasteBox = CheckBox(this).apply {
            text = "Or paste a list instead"
            isChecked = prefs.getBoolean("rl_paste", false)
            setOnCheckedChangeListener { _, on -> rlText.visibility = if (on) View.VISIBLE else View.GONE }
        }
        rlText.visibility = if (rlPasteBox.isChecked) View.VISIBLE else View.GONE
        c.addView(rlPasteBox)
        c.addView(rlText)

        loadBtn = button("1. Load") { loadList() }
        findBtn = button("2. Find") { startJob("rl_find", settingsJson(), false) }
        dlBtn = button("3. Download") { downloadList() }
        c.addView(row(loadBtn, findBtn, dlBtn))
        savedBtn = button("Open a saved list") { openSaved() }
        c.addView(savedBtn)
        rlTitle = label("", 15f, true, Color.BLACK)
        rlSummary = label("", 12f)
        c.addView(rlTitle)
        c.addView(rlSummary)

        rlList = ListView(this).apply {
            adapter = rowAdapter
            onItemClickListener = AdapterView.OnItemClickListener { _, _, pos, _ ->
                rows.optJSONObject(pos)?.let { openMatch(it.getString("key")) }
            }
            onItemLongClickListener = AdapterView.OnItemLongClickListener { _, _, pos, _ ->
                rows.optJSONObject(pos)?.let { call("rl_toggle", it.getString("key")) }
                true
            }
        }
        val page = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        page.addView(c)
        page.addView(label("  Tap a row to change its match. Long-press to skip or include it.", 11f))
        page.addView(rlList, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        return page
    }

    private fun buildSettings(): View {
        val c = column()
        c.addView(label("Save comics in"))
        @Suppress("DEPRECATION")
        val defaultRoot = Environment.getExternalStorageDirectory().absolutePath + "/Comics"
        rootEdit = edit(defaultRoot, pref("final_root", defaultRoot))
        c.addView(rootEdit)
        accessText = label("", 12f)
        c.addView(accessText)
        c.addView(button("Allow saving to this folder") { askStorageAccess() })

        c.addView(label("Files per folder (0 = one folder)"))
        perFolderEdit = edit("100", pref("files_per_folder", "100"), number = true)
        c.addView(perFolderEdit)
        c.addView(label("Big downloads go in subfolders like \"001-100\", \"101-200\" so folders open fast.", 12f))

        verboseBox = CheckBox(this).apply {
            text = "Show details in the progress box"
            isChecked = prefs.getBoolean("verbose", false)
            setOnCheckedChangeListener { _, on ->
                prefs.edit().putBoolean("verbose", on).apply()
                call("set_verbose", on)
            }
        }
        c.addView(verboseBox)

        c.addView(label("Browser", 15f, true, Color.BLACK))
        c.addView(button("Show the built-in browser") { showBrowser(true) })
        c.addView(button("Clear browser cookies (fixes stuck \"verify you are human\" loops)") {
            CookieManager.getInstance().removeAllCookies(null)
            CookieManager.getInstance().flush()
            toast("Cookies cleared")
        })
        c.addView(label("Comics are saved as .cbz files. Open them with a comic reader app such as " +
            "ComicScreen, Mihon or Perfect Viewer.", 12f))
        return ScrollView(this).apply { addView(c) }
    }

    private fun showTab(i: Int) {
        val index = if (i in 0..2) i else 0
        tabViews.forEachIndexed { n, v -> v.visibility = if (n == index) View.VISIBLE else View.GONE }
        tabButtons.forEachIndexed { n, b ->
            b.setTypeface(null, if (n == index) Typeface.BOLD else Typeface.NORMAL)
            b.alpha = if (n == index) 1f else 0.6f
        }
        prefs.edit().putInt("tab", index).apply()
        if (index == 2) updateAccessText()
    }

    fun showBrowser(show: Boolean) {
        browserShown = show
        panel.visibility = if (show) View.GONE else View.VISIBLE
        browserBar.visibility = if (show) View.VISIBLE else View.GONE
    }

    @Deprecated("Deprecated in Java")
    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        if (browserShown) {
            showBrowser(false)
        } else {
            super.onBackPressed()
        }
    }

    // =================================================================== talking to Python
    private fun call(fn: String, vararg args: Any?, then: ((String) -> Unit)? = null) {
        py.execute {
            val e0 = engine
            val out = try {
                if (e0 == null) "The engine is still starting. Try again in a moment."
                else e0.callAttr(fn, *args)?.toString() ?: ""          // Python None comes back as null
            } catch (e: Exception) {
                "Error: ${e.message}"
            }
            if (then != null) main.post { then(out) }
        }
    }

    fun onPyEvent(name: String) {
        main.post {
            when (name) {
                "table" -> refreshTable()
                "candidates" -> matchDialog?.reload()
                "done" -> {
                    setBusy(false)
                    refreshTable()
                }
            }
        }
    }

    fun appendLog(text: String) {
        synchronized(logBuf) { logBuf.append(text) }
        if (logPending.compareAndSet(false, true)) {
            main.postDelayed({ flushLog() }, 150)
        }
    }

    private fun flushLog() {
        logPending.set(false)
        val text = synchronized(logBuf) {
            val t = logBuf.toString()
            logBuf.setLength(0)
            t
        }
        if (text.isEmpty()) return
        logView.append(text)
        val all = logView.text
        if (all.length > 150_000) logView.text = all.subSequence(all.length - 100_000, all.length)
        logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
    }

    private fun settingsJson(): String {
        saveSettings()
        val o = JSONObject()
        o.put("start_url", linkEdit.text.toString().trim())
        o.put("start_issue", startEdit.text.toString().trim())
        o.put("end_issue", endEdit.text.toString().trim())
        o.put("all_issues", allBox.isChecked)
        o.put("folder_name", nameEdit.text.toString().trim())
        o.put("sub_path", groupEdit.text.toString().trim())
        o.put("filename_prefix", prefixEdit.text.toString().trim())
        o.put("final_root", rootEdit.text.toString().trim())
        o.put("files_per_folder", perFolderEdit.text.toString().trim())
        o.put("verbose", verboseBox.isChecked)
        return o.toString()
    }

    private fun saveSettings() {
        prefs.edit()
            .putString("start_url", linkEdit.text.toString().trim())
            .putString("start_issue", startEdit.text.toString().trim())
            .putString("end_issue", endEdit.text.toString().trim())
            .putBoolean("all_issues", allBox.isChecked)
            .putString("folder_name", nameEdit.text.toString().trim())
            .putString("sub_path", groupEdit.text.toString().trim())
            .putString("filename_prefix", prefixEdit.text.toString().trim())
            .putString("final_root", rootEdit.text.toString().trim())
            .putString("files_per_folder", perFolderEdit.text.toString().trim())
            .putString("rl_url", rlUrl.text.toString().trim())
            .putString("rl_text", rlText.text.toString())
            .putBoolean("rl_paste", rlPasteBox.isChecked)
            .apply()
    }

    private fun checkLink() {
        call("check_link", linkEdit.text.toString()) { json ->
            try {
                val a = JSONArray(json)
                linkCheck.text = a.getString(1)
                linkCheck.setTextColor(if (a.getBoolean(0)) Color.rgb(46, 125, 50) else Color.rgb(198, 40, 40))
            } catch (e: Exception) {
                linkCheck.text = ""
            }
        }
    }

    // =================================================================== jobs
    private fun startJob(fn: String, vararg args: Any?) {
        if (busy) {
            toast("Wait for the current job to finish, or tap Stop.")
            return
        }
        if (engine == null) {
            toast("Still starting up. Try again in a moment.")
            return
        }
        if (!hasStorageAccess()) {
            askStorageAccess()
            return
        }
        askNotificationsOnce()
        logView.text = ""
        setBusy(true)
        call(fn, *args) { msg ->
            if (msg.isNotEmpty() && msg != "None") {
                setBusy(false)
                alert(msg)
            }
        }
    }

    private fun stopJob() {
        call("stop")
        stopBtn.isEnabled = false
    }

    private fun setBusy(b: Boolean) {
        busy = b
        startBtn.isEnabled = !b
        loadBtn.isEnabled = !b
        savedBtn.isEnabled = !b
        findBtn.isEnabled = !b && hasPlan
        dlBtn.isEnabled = !b && hasPlan
        stopBtn.isEnabled = b
        if (b) {
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            val i = Intent(this, JobService::class.java)
            try {
                if (Build.VERSION.SDK_INT >= 26) startForegroundService(i) else startService(i)
            } catch (e: Exception) {
                appendLog("(couldn't start the background notification: ${e.message})\n")
            }
        } else {
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            stopService(Intent(this, JobService::class.java))
            if (browserShown) showBrowser(false)
        }
    }

    private fun loadList() {
        val text = if (rlPasteBox.isChecked) rlText.text.toString() else ""
        val url = if (rlPasteBox.isChecked) "" else rlUrl.text.toString()
        startJob("rl_load", settingsJson(), url, text)
    }

    private fun downloadList() {
        call("rl_unmatched") { json ->
            val names = try { JSONArray(json) } catch (e: Exception) { JSONArray() }
            if (names.length() == 0) {
                startJob("rl_download", settingsJson())
            } else {
                val shown = (0 until minOf(names.length(), 12)).joinToString("\n") { names.getString(it) }
                AlertDialog.Builder(this)
                    .setTitle("Some series have no match")
                    .setMessage("${names.length()} series have no Batcave match and will be skipped:\n\n$shown" +
                        (if (names.length() > 12) "\n..." else "") + "\n\nDownload the rest anyway?")
                    .setPositiveButton("Download") { _, _ -> startJob("rl_download", settingsJson()) }
                    .setNegativeButton("Cancel", null)
                    .show()
            }
        }
    }

    private fun openSaved() {
        call("rl_saved_names") { json ->
            val arr = try { JSONArray(json) } catch (e: Exception) { JSONArray() }
            if (arr.length() == 0) {
                toast("No saved lists yet. Load one first.")
                return@call
            }
            val names = Array(arr.length()) { arr.getString(it) }
            AlertDialog.Builder(this)
                .setTitle("Saved reading lists")
                .setItems(names) { _, which ->
                    call("rl_open", names[which]) { refreshTable() }
                }
                .show()
        }
    }

    private fun refreshTable() {
        call("rl_table") { json ->
            try {
                val o = JSONObject(json)
                hasPlan = o.optBoolean("has_plan")
                rlTitle.text = o.optString("title")
                rlSummary.text = o.optString("summary")
                rows = o.optJSONArray("rows") ?: JSONArray()
                rowAdapter.notifyDataSetChanged()
                val name = o.optString("name")
                if (name.isNotEmpty()) prefs.edit().putString("rl_last", name).apply()
                setBusy(busy)
            } catch (e: Exception) {
                // engine not ready yet
            }
        }
    }

    // =================================================================== storage + notifications
    private fun hasStorageAccess(): Boolean {
        val root = rootEdit.text.toString()
        val own = getExternalFilesDir(null)?.absolutePath
        if (root.startsWith(filesDir.absolutePath) || (own != null && root.startsWith(own))) return true
        return if (Build.VERSION.SDK_INT >= 30) {
            Environment.isExternalStorageManager()
        } else {
            checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED
        }
    }

    private fun askStorageAccess() {
        AlertDialog.Builder(this)
            .setTitle("Allow saving comics")
            .setMessage("Comic Scraper saves comics in ${rootEdit.text}, where your comic reader apps can open them. " +
                "Android needs you to allow that once.\n\nOn the next screen, turn on \"Allow access to manage all files\" " +
                "for Comic Scraper, then come back and try again.")
            .setPositiveButton("Allow") { _, _ ->
                if (Build.VERSION.SDK_INT >= 30) {
                    try {
                        startActivity(Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                            Uri.parse("package:$packageName")))
                    } catch (e: Exception) {
                        startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
                    }
                } else {
                    requestPermissions(arrayOf(Manifest.permission.WRITE_EXTERNAL_STORAGE), 2)
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun updateAccessText() {
        if (hasStorageAccess()) {
            accessText.text = "Saving is allowed."
            accessText.setTextColor(Color.rgb(46, 125, 50))
        } else {
            accessText.text = "Not allowed yet: tap the button below."
            accessText.setTextColor(Color.rgb(198, 40, 40))
        }
    }

    private fun askNotificationsOnce() {
        if (Build.VERSION.SDK_INT >= 33 && !prefs.getBoolean("asked_notifications", false) &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            prefs.edit().putBoolean("asked_notifications", true).apply()
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 3)
        }
    }

    override fun onResume() {
        super.onResume()
        if (::accessText.isInitialized) updateAccessText()
    }

    override fun onPause() {
        super.onPause()
        CookieManager.getInstance().flush()
        if (::linkEdit.isInitialized) saveSettings()
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()

    private fun alert(msg: String) {
        AlertDialog.Builder(this).setMessage(msg).setPositiveButton("OK", null).show()
    }

    // =================================================================== reading-list rows
    inner class RowAdapter : BaseAdapter() {
        override fun getCount(): Int = rows.length()
        override fun getItem(position: Int): Any = rows.opt(position) ?: ""
        override fun getItemId(position: Int): Long = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup?): View {
            val box = (convertView as? LinearLayout) ?: LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(12), dp(8), dp(12), dp(8))
                addView(TextView(this@MainActivity).apply { textSize = 14f })
                addView(TextView(this@MainActivity).apply { textSize = 12f })
            }
            val r = rows.optJSONObject(position) ?: return box
            val color = when (r.optString("color")) {
                "missing" -> Color.rgb(198, 40, 40)
                "check" -> Color.rgb(178, 106, 0)
                "soon" -> Color.rgb(21, 101, 192)
                "off" -> Color.rgb(158, 158, 158)
                else -> Color.rgb(33, 33, 33)
            }
            val top = box.getChildAt(0) as TextView
            val bottom = box.getChildAt(1) as TextView
            val found = r.optString("found")
            top.text = r.optString("name") + "  (" + r.optInt("count") + ")" +
                (if (found.isNotEmpty()) "   found $found" else "")
            val match = r.optString("match").ifEmpty { "no match" }
            bottom.text = match + "   -   " + r.optString("status")
            top.setTextColor(color)
            bottom.setTextColor(color)
            return box
        }
    }

    private fun openMatch(key: String) {
        matchDialog = MatchDialog(key)
    }

    /** Pick, search for, or paste the Batcave series for one row of the reading list. */
    inner class MatchDialog(private val key: String) {
        private val heading = label("", 14f, true, Color.BLACK)
        private val note = label("", 12f)
        private val items = ArrayList<String>()
        private val listAdapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_list_item_single_choice, items)
        private val list = ListView(this@MainActivity)
        private val query = edit("Search Batcave for...", "")
        private val link = edit("Or paste its Batcave series page", "")
        private val dialog: AlertDialog
        private var firstLoad = true

        init {
            val box = column()
            box.addView(heading)
            box.addView(note)
            box.addView(label("Pick the right series:"))
            list.adapter = listAdapter
            list.choiceMode = ListView.CHOICE_MODE_SINGLE
            box.addView(list, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(220)))
            val searchRow = LinearLayout(this@MainActivity).apply { orientation = LinearLayout.HORIZONTAL }
            searchRow.addView(query, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            searchRow.addView(button("Search") { search() })
            box.addView(searchRow)
            box.addView(link)
            dialog = AlertDialog.Builder(this@MainActivity)
                .setTitle("Batcave match")
                .setView(ScrollView(this@MainActivity).apply { addView(box) })
                .setPositiveButton("Use this", null)
                .setNeutralButton("Skip series", null)
                .setNegativeButton("Cancel", null)
                .create()
            dialog.setOnShowListener {
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener { use() }
                dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener {
                    call("rl_skip", key) { dialog.dismiss() }
                }
            }
            dialog.setOnDismissListener { if (matchDialog === this) matchDialog = null }
            dialog.show()
            reload()
        }

        fun reload() {
            call("rl_details", key) { json ->
                try {
                    val o = JSONObject(json)
                    heading.text = o.optString("heading")
                    note.text = o.optString("note")
                    note.visibility = if (note.text.isEmpty()) View.GONE else View.VISIBLE
                    if (firstLoad) {
                        query.setText(o.optString("query"))
                        firstLoad = false
                    }
                    items.clear()
                    val c = o.optJSONArray("candidates") ?: JSONArray()
                    var current = -1
                    for (i in 0 until c.length()) {
                        val x = c.getJSONObject(i)
                        val isCurrent = x.optBoolean("current")
                        items.add(x.optString("title") + if (isCurrent) "   (current)" else "")
                        if (isCurrent) current = i
                    }
                    if (items.isEmpty()) items.add("(no results yet: search below or paste a link)")
                    listAdapter.notifyDataSetChanged()
                    list.clearChoices()
                    if (current >= 0) list.setItemChecked(current, true)
                } catch (e: Exception) {
                    heading.text = "This row is gone. Reload the list."
                }
            }
        }

        private fun search() {
            if (busy) {
                toast("Wait for the current job to finish, or tap Stop.")
                return
            }
            setBusy(true)
            call("rl_search", settingsJson(), key, query.text.toString()) { msg ->
                if (msg.isNotEmpty() && msg != "None") {
                    setBusy(false)
                    toast(msg)
                }
            }
        }

        private fun use() {
            val idx = list.checkedItemPosition
            call("rl_use", key, if (idx == ListView.INVALID_POSITION) -1 else idx, link.text.toString()) { msg ->
                if (msg.isEmpty() || msg == "None") dialog.dismiss() else toast(msg)
            }
        }
    }
}
