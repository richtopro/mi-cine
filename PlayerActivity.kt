package es.micine.app

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.KeyEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import org.videolan.libvlc.LibVLC
import org.videolan.libvlc.Media
import org.videolan.libvlc.MediaPlayer
import org.videolan.libvlc.util.VLCVideoLayout

/**
 * Reproductor con el motor de VLC: reproduce MKV, MP4, x264, x265/HEVC, AC3, DTS, subtítulos…
 * Mando: ← −10 s · → +30 s · OK pausa/controles · ↑/↓ muestra los controles.
 */
class PlayerActivity : Activity() {

    private lateinit var libVLC: LibVLC
    private lateinit var player: MediaPlayer
    private lateinit var video: VLCVideoLayout
    private lateinit var controls: View
    private lateinit var seek: SeekBar
    private lateinit var timeTxt: TextView
    private lateinit var btnPlay: Button
    private lateinit var btnNext: Button

    private val handler = Handler(Looper.getMainLooper())
    private var released = false
    private var ended = false
    private var finishing = false
    private var lengthMs = 0L
    private var lastPosMs = 0L
    private var dragging = false

    private val hideControls = Runnable { controls.visibility = View.GONE; immersive() }
    private val ticker = object : Runnable {
        override fun run() {
            if (released) return
            val len = player.getLength()
            if (len > 0) lengthMs = len
            val pos = player.getTime()
            if (pos > 0) lastPosMs = pos
            if (!dragging && lengthMs > 0) {
                seek.max = (lengthMs / 1000).toInt()
                seek.progress = (lastPosMs / 1000).toInt()
                timeTxt.text = fmt(lastPosMs / 1000) + " / " + fmt(lengthMs / 1000)
            }
            handler.postDelayed(this, 500)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or WindowManager.LayoutParams.FLAG_FULLSCREEN)
        setContentView(R.layout.activity_player)
        immersive()

        video = findViewById(R.id.video)
        controls = findViewById(R.id.controls)
        seek = findViewById(R.id.seek)
        timeTxt = findViewById(R.id.time)
        btnPlay = findViewById(R.id.btnPlay)
        btnNext = findViewById(R.id.btnNext)

        val title = listOf(intent.getStringExtra("title"), intent.getStringExtra("subtitle"))
            .filter { !it.isNullOrBlank() }.joinToString(" · ")
        findViewById<TextView>(R.id.title).text = title
        btnNext.visibility = if (intent.getBooleanExtra("hasNext", false)) View.VISIBLE else View.GONE

        val opts = arrayListOf(
            "--http-reconnect",
            "--network-caching=3000",
            "--audio-time-stretch",
            "--audio-language=spa,es,esp,spanish,cas",
            "--sub-language=none"
        )
        libVLC = LibVLC(this, opts)
        player = MediaPlayer(libVLC)
        player.attachViews(video, null, true, false)

        val media = Media(libVLC, Uri.parse(intent.getStringExtra("url")))
        media.setHWDecoderEnabled(true, false)
        val start = intent.getLongExtra("start", 0L)
        if (start > 0) media.addOption(":start-time=$start")
        player.setMedia(media)
        media.release()

        player.setEventListener(object : MediaPlayer.EventListener {
            override fun onEvent(event: MediaPlayer.Event) {
                when (event.type) {
                    MediaPlayer.Event.EndReached -> handler.post {
                        ended = true
                        done(intent.getBooleanExtra("hasNext", false))
                    }
                    MediaPlayer.Event.EncounteredError -> handler.post {
                        Toast.makeText(this@PlayerActivity, "No se pudo reproducir el vídeo", Toast.LENGTH_LONG).show()
                        done(false)
                    }
                    MediaPlayer.Event.Playing, MediaPlayer.Event.Paused -> handler.post { updatePlay() }
                }
            }
        })

        btnPlay.setOnClickListener { togglePause() }
        findViewById<Button>(R.id.btnBack).setOnClickListener { seekBy(-10) }
        findViewById<Button>(R.id.btnFwd).setOnClickListener { seekBy(30) }
        findViewById<Button>(R.id.btnAudio).setOnClickListener { chooseAudio() }
        findViewById<Button>(R.id.btnSubs).setOnClickListener { chooseSubs() }
        btnNext.setOnClickListener { ended = true; done(true) }
        video.setOnClickListener { if (controls.visibility == View.VISIBLE) hideNow() else showControls() }

        seek.keyProgressIncrement = 10
        seek.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(sb: SeekBar, progress: Int, fromUser: Boolean) {
                if (!fromUser) return
                timeTxt.text = fmt(progress.toLong()) + " / " + fmt(lengthMs / 1000)
                if (!dragging) seekTo(progress * 1000L) // mando (flechas sobre la barra)
                showControls()
            }
            override fun onStartTrackingTouch(sb: SeekBar) { dragging = true; handler.removeCallbacks(hideControls) }
            override fun onStopTrackingTouch(sb: SeekBar) { dragging = false; seekTo(sb.progress * 1000L); showControls() }
        })

        player.play()
        handler.post(ticker)
        showControls()
    }

    private fun fmt(totalSec: Long): String {
        val s = if (totalSec < 0) 0 else totalSec
        val h = s / 3600; val m = (s % 3600) / 60; val sec = s % 60
        return if (h > 0) String.format("%d:%02d:%02d", h, m, sec) else String.format("%d:%02d", m, sec)
    }

    @Suppress("DEPRECATION")
    private fun immersive() {
        window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            or View.SYSTEM_UI_FLAG_FULLSCREEN
            or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
            or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN)
    }

    private fun showControls() {
        val wasHidden = controls.visibility != View.VISIBLE
        controls.visibility = View.VISIBLE
        if (wasHidden) btnPlay.requestFocus()
        handler.removeCallbacks(hideControls)
        handler.postDelayed(hideControls, 5000)
    }

    private fun hideNow() {
        handler.removeCallbacks(hideControls)
        hideControls.run()
    }

    private fun updatePlay() {
        if (released) return
        btnPlay.text = if (player.isPlaying()) "❚❚  Pausa" else "▶  Seguir"
    }

    private fun togglePause() {
        if (released) return
        if (player.isPlaying()) player.pause() else player.play()
        showControls()
    }

    private fun seekTo(ms: Long) {
        if (released) return
        val target = if (lengthMs > 5000) ms.coerceIn(0L, lengthMs - 2000) else ms.coerceAtLeast(0L)
        player.setTime(target)
        lastPosMs = target
    }

    private fun seekBy(sec: Int) {
        seekTo(lastPosMs + sec * 1000L)
        Toast.makeText(this, if (sec > 0) "+$sec s" else "$sec s", Toast.LENGTH_SHORT).show()
    }

    private fun chooseAudio() {
        val tracks = player.getAudioTracks() ?: return toast("No hay otras pistas de audio")
        val names = tracks.map { it.name ?: "Pista ${it.id}" }.toTypedArray()
        val cur = tracks.indexOfFirst { it.id == player.getAudioTrack() }
        AlertDialog.Builder(this).setTitle("Audio")
            .setSingleChoiceItems(names, cur) { d, w -> player.setAudioTrack(tracks[w].id); d.dismiss() }
            .show()
    }

    private fun chooseSubs() {
        val tracks = player.getSpuTracks() ?: return toast("Este vídeo no tiene subtítulos")
        val names = tracks.map { if (it.id == -1) "Desactivados" else (it.name ?: "Pista ${it.id}") }.toTypedArray()
        val cur = tracks.indexOfFirst { it.id == player.getSpuTrack() }
        AlertDialog.Builder(this).setTitle("Subtítulos")
            .setSingleChoiceItems(names, cur) { d, w -> player.setSpuTrack(tracks[w].id); d.dismiss() }
            .show()
    }

    private fun toast(msg: String) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        val visible = controls.visibility == View.VISIBLE
        when (keyCode) {
            KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE, KeyEvent.KEYCODE_MEDIA_PLAY, KeyEvent.KEYCODE_MEDIA_PAUSE -> { togglePause(); return true }
            KeyEvent.KEYCODE_MEDIA_FAST_FORWARD -> { seekBy(30); return true }
            KeyEvent.KEYCODE_MEDIA_REWIND -> { seekBy(-10); return true }
            KeyEvent.KEYCODE_MEDIA_NEXT -> if (btnNext.visibility == View.VISIBLE) { ended = true; done(true); return true }
        }
        if (!visible) {
            when (keyCode) {
                KeyEvent.KEYCODE_DPAD_LEFT -> { seekBy(-10); return true }
                KeyEvent.KEYCODE_DPAD_RIGHT -> { seekBy(30); return true }
                KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER -> { togglePause(); return true }
                KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_DPAD_DOWN, KeyEvent.KEYCODE_MENU -> { showControls(); return true }
            }
        } else {
            handler.removeCallbacks(hideControls)
            handler.postDelayed(hideControls, 5000)
        }
        return super.onKeyDown(keyCode, event)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (controls.visibility == View.VISIBLE) hideNow() else done(false)
    }

    private fun done(next: Boolean) {
        if (finishing) return
        finishing = true
        val pos = if (ended) lengthMs else lastPosMs
        val data = Intent()
            .putExtra("fileId", intent.getStringExtra("fileId"))
            .putExtra("key", intent.getStringExtra("key"))
            .putExtra("pos", pos / 1000)
            .putExtra("dur", lengthMs / 1000)
            .putExtra("ended", ended)
            .putExtra("next", next)
        setResult(RESULT_OK, data)
        finish()
    }

    override fun onStop() {
        super.onStop()
        if (!released && player.isPlaying()) player.pause()
    }

    override fun onDestroy() {
        super.onDestroy()
        handler.removeCallbacksAndMessages(null)
        if (!released) {
            released = true
            try {
                player.stop()
                player.detachViews()
                player.release()
                libVLC.release()
            } catch (e: Exception) {
                e.printStackTrace()
            }
        }
    }
}
