-- frame-accurate position beacon: mpv 0.41's ${time-pos} in the status
-- line is OSD-cached (~1 Hz, seconds stale) while percent-pos is integer;
-- neither drives the seek bars. observe_property fires on every real
-- position change, so we throttle to ~10 Hz and print fresh positions.
local last_pos = -9
mp.observe_property("time-pos", "number", function(name, pos)
    if pos == nil then return end
    if math.abs(pos - last_pos) >= 0.099 then
        last_pos = pos
        print(string.format("SYNCPOS|%.3f", pos))
    end
end)
mp.observe_property("eof-reached", "bool", function(name, v)
    print("SYNCEOF|" .. tostring(v))
end)
mp.observe_property("pause", "bool", function(name, value)
    if mp.get_property_bool("eof-reached") then
        print("SYNCPAUSE|eof")
    else
        print("SYNCPAUSE|" .. tostring(value))
    end
end)
-- integrated PiP: report left-button drag state so the app can follow the
-- pane while the user drags it (mpv window-dragging moves the window).
local dragging = false
mp.observe_property("mouse-pos", "native", function()
    if mp.get_property_bool("mouse-btn1-down", false) then
        if not dragging then
            dragging = true
            print("SYNCPIPDRAG|start")
        end
    else
        if dragging then
            dragging = false
            print("SYNCPIPDRAG|end")
        end
    end
end)
mp.register_script_message("pip-undock", function()
    print("SYNCPIPDRAG|undock")
end)
-- click-to-pause with double-click discrimination: a left click arms a
-- deferred pause (450 ms); a double-click (MBTN_LEFT_DBL, handled below)
-- cancels it, so double-clicking a video fullscreens it WITHOUT pausing
-- and the two videos never desync (the old MBTN_LEFT cycle pause fired
-- on the first press of a double-click and left one video paused).
local sp_click_t = nil
local sp_click_fs = false
-- nameless bindings: input.conf owns the keys via `script-binding`
mp.add_key_binding("", "syncplayer-click", function()
    if sp_click_t then sp_click_t:kill() end
    sp_click_fs = mp.get_property_bool("fullscreen")
    sp_click_t = mp.add_timeout(0.45, function()
        sp_click_t = nil
        -- double-click safety net: if fullscreen changed since the press
        -- (the DBL handler / property observer may lag under load), this
        -- was a double-click -> do NOT pause
        if mp.get_property_bool("fullscreen") ~= sp_click_fs then
            return
        end
        mp.command("cycle pause")
    end)
end)
-- Double-click handler (dispatched by input.conf via `script-binding`, so
-- it owns the input EVENT - the fullscreen property apply can take ~0.5 s
-- on high-DPI displays, which would race any property-based cancel): it
-- cancels the pending deferred pause and cycles fullscreen.
mp.add_key_binding("", "syncplayer-dbl", function()
    if sp_click_t then sp_click_t:kill() end
    sp_click_t = nil
    mp.command("cycle fullscreen")
end)
-- belt and suspenders: if fullscreen changes through ANY other path,
-- cancel the pending click-pause too
mp.observe_property("fullscreen", "bool", function(name, v)
    if v ~= nil and sp_click_t then
        sp_click_t:kill()
        sp_click_t = nil
    end
end)
