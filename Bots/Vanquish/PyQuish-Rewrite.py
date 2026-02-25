# ============================================================================
# PyQuish - Dynamic Map Selection Vanquishing Bot
# ============================================================================
# This bot allows users to select any available map from the PyQuishAI_maps
# directory structure and automatically vanquish it using the appropriate outpost and path data.
# ============================================================================

from Py4GWCoreLib import Botting, Routines, GLOBAL_CACHE, ModelID, Agent, Player, ConsoleLog, AgentArray, Utils, Range, Timer, Map, AutoPathing
import Py4GW
import PyImGui
import os

# ============================================================================
# Configuration
# ============================================================================

BOT_NAME = "PyQuish"
MAPS_DIR = os.path.join(Py4GW.Console.get_projects_path(), "Sources", "aC_Scripts", "PyQuishAI_maps")

# ============================================================================
# Bot State Management
# ============================================================================

class BotVars:
    """Container for bot runtime variables and selected map data"""
    def __init__(self):
        self.selected_region = ""
        self.selected_map = ""
        self.current_outpost_id = 0
        self.current_map_id = 0
        self.outpost_path = []  # Path from outpost to map entrance
        self.bless_path_data = []  # List of {"bless": (x,y), "path": [(x,y), ...]} or simple path list
        self.waypoint_states: list[tuple[str, float, float]] = []  # (state_name, x, y) for each XY waypoint in order
        self.use_hero_ai = False  # Whether to enable HeroAI for hero combat management (opt-in)
        self.use_enemy_scanner = True  # Whether to enable the background enemy scanner (opt-out)

bot_vars = BotVars()
bot = Botting(BOT_NAME)
routine_set = False  # Tracks if FSM has been initialized
needs_routine_init = False  # Flag to trigger FSM rebuild

# Mutable flag: True while the enemy scanner has paused and owns the FSM.
# Used by _on_death to avoid conflicting with scanner's own resume logic.
_scanner_has_fsm_control = [False]

# After the scanner resumes the FSM, suppress new scans until the FSM state
# changes (player reached the current waypoint) OR the timeout expires.
# This prevents the scanner from immediately re-pausing the FSM while the
# player is still walking back toward the waypoint from a combat position.
_scanner_resume_suppress_state: list[str | None] = [None]   # FSM state name at time of resume
_scanner_resume_suppress_ts: list[int] = [0]                 # timestamp of resume
SCANNER_RESUME_SUPPRESS_TIMEOUT_MS = 600000  # 10-minute safety-net timeout
SCANNER_RESUME_CLEAR_DISTANCE = 300          # clear suppress when player is this close to target waypoint

# ============================================================================
# Bot Routine
# ============================================================================

def bot_routine(bot: Botting) -> None:
    """
    Build the FSM for vanquishing the selected map.
    This is called when config.initialized == False during bot.Update().
    IDs are captured at build time from bot_vars.
    """
    target_outpost = bot_vars.current_outpost_id
    target_map = bot_vars.current_map_id
    map_name = f"{bot_vars.selected_region}/{bot_vars.selected_map}"
    
    ConsoleLog("Bot", f"Building FSM for: {map_name} (Outpost: {target_outpost}, Map: {target_map})")
    
    # Clear waypoint map in case this routine is called more than once
    bot_vars.waypoint_states.clear()
    
    # Validate outpost ID
    if target_outpost == 0:
        ConsoleLog("Bot", "ERROR: Outpost ID is 0, cannot proceed!", message_type=6)
        bot.States.AddHeader("ERROR")
        return
    
    # Build FSM states
    bot.States.AddHeader(BOT_NAME)
    
    # Register death callback
    death_callback = lambda: on_death(bot)
    bot.Events.OnDeathCallback(death_callback)
    
    # Enable halt on death property
    bot.Properties.Enable("halt_on_death")
    
    bot.Templates.Multibox_Aggressive()
    if not bot_vars.use_hero_ai:
        bot.Properties.Disable("hero_ai")
        ConsoleLog("Bot", "HeroAI disabled (user setting)")
    else:
        ConsoleLog("Bot", "HeroAI enabled")
    bot.Properties.Enable("auto_combat")
    ConsoleLog("Bot", "Player auto-combat enabled (skills + attacks)")
    if bot_vars.use_enemy_scanner:
        # Scanner handles all combat engagement; pause_on_danger causes permanent
        # deadlocks after a party wipe because IsPartyMemberDead() stays True.
        bot.Properties.Disable("pause_on_danger")
        ConsoleLog("Bot", "pause_on_danger disabled (scanner handles combat)")
    else:
        # No scanner - let the library pause movement on danger so auto_combat
        # can kill enemies before continuing, same as the Norn title farmer.
        bot.Properties.Enable("pause_on_danger")
        ConsoleLog("Bot", "pause_on_danger enabled (scanner disabled, library handles combat pausing)")
    # Disable auto_loot to prevent loot_pause() from halting movement when the
    # player walks through areas with mob drops after combat.
    bot.Properties.Disable("auto_loot")
    ConsoleLog("Bot", "auto_loot disabled (no looting during vanquish path)")
    
    # Travel to outpost
    if bot_vars.use_hero_ai:
        # Multibox mode: kick/summon/invite all accounts then travel
        bot.Templates.Routines.PrepareForFarm(map_id_to_travel=target_outpost)
    else:
        # Solo mode: just travel to outpost, no multibox linking
        bot.States.AddHeader("Prepare For Farm")
        bot.Map.Travel(target_map_id=target_outpost)

    # Navigate outpost to exit using dynamic path
    bot.Party.SetHardMode(True)
    if bot_vars.outpost_path:
        ConsoleLog("Bot", f"Following outpost path with {len(bot_vars.outpost_path)} waypoints")
        bot.Move.FollowPath(bot_vars.outpost_path)
    else:
        ConsoleLog("Bot", "WARNING: No outpost path defined!", message_type=6)
    bot.Wait.ForMapLoad(target_map_id=target_map)
    
    bot.States.AddHeader("Vanquishing")
    
    # Helper function to start enemy scanner
    def start_scanner():
        if not bot_vars.use_enemy_scanner:
            ConsoleLog("Bot", "Enemy scanner disabled (user setting)")
            return
        scanner = _enemy_scanner_coroutine(bot)
        bot.config.FSM.AddManagedCoroutine("EnemyScanner", scanner)
        ConsoleLog("Bot", "Enemy scanner active - will detect nearby enemies")
    
    scanner_started = False

    def _add_move_xy(x: float, y: float, step_name: str = "") -> None:
        """Add a movement waypoint state.

        Scanner enabled: custom coroutine using AutoPathing + FollowPath with
        custom_pause_fn so the scanner owns all pausing decisions.

        Scanner disabled: delegates directly to bot.Move.XY(), identical to the
        Norn title farmer, letting pause_on_danger handle combat pausing natively.
        """
        name = step_name or f"MoveTo_{x:.0f}_{y:.0f}"

        if not bot_vars.use_enemy_scanner:
            # Identical to how the Norn title farmer moves between waypoints.
            bot.Move.XY(x, y, name)
            return

        # Scanner-enabled path: AutoPathing coroutine with custom pause so the
        # scanner (not pause_on_danger) owns all FSM pausing.
        _fsm = bot.config.FSM

        def _coro(wx: float = float(x), wy: float = float(y)):
            from Py4GWCoreLib.Pathing import AutoPathing
            from Py4GWCoreLib.Routines import Routines
            # Compute AutoPath from current position to the waypoint.
            path = yield from AutoPathing().get_path_to(wx, wy)
            bot.config.path = list(path)  # keep config.path updated for UI drawing
            # stop_on_party_wipe=False: without this, FollowPath returns False on
            # every call after a party wipe because IsPartyDefeated() stays True.
            yield from Routines.Yield.Movement.FollowPath(
                path_points=list(path),
                custom_exit_condition=lambda: (
                    not Routines.Checks.Map.MapValid()
                    or Routines.Checks.Player.IsDead()
                ),
                custom_pause_fn=lambda: _fsm.is_paused(),
                timeout=-1,
                tolerance=150,
                stop_on_party_wipe=False,
            )

        bot.States.AddCustomState(_coro, name)

    if bot_vars.bless_path_data:
        if isinstance(bot_vars.bless_path_data, list) and len(bot_vars.bless_path_data) > 0:
            first_element = bot_vars.bless_path_data[0]
            
            # Format 1: List of dicts with "bless" and "path" keys (e.g., BarbarousShore)
            if isinstance(first_element, dict):
                ConsoleLog("Bot", f"Processing {len(bot_vars.bless_path_data)} blessing/path segments")
                
                for idx, segment in enumerate(bot_vars.bless_path_data):
                    bless_coord = segment.get("bless")
                    combat_path = segment.get("path", [])
                    
                    if bless_coord:
                        # Move to and interact with blessing shrine
                        bot.Move.XY(bless_coord[0], bless_coord[1], f"Move to blessing {idx + 1}")
                        bot.Wait.ForTime(5000)
                        bot.Move.XYAndInteractNPC(bless_coord[0], bless_coord[1])
                        bot.Wait.ForTime(1000)
                        
                        # Send dialog options for all campaign blessings
                        bot.Multibox.SendDialogToTarget(0x84)  # EOTN Map blessing
                        bot.Multibox.SendDialogToTarget(0x85)  # Nightfall Map blessing
                        bot.Multibox.SendDialogToTarget(0x86)  # Factions Map blessing
                        bot.Wait.ForTime(10000)
                        
                        # Start enemy scanner after first blessing is collected
                        if not scanner_started:
                            bot.States.AddCustomState(start_scanner, "Start Enemy Scanner")
                            scanner_started = True
                    
                    # If no blessing in first segment, start scanner before combat path
                    if not scanner_started and idx == 0 and combat_path:
                        bot.States.AddCustomState(start_scanner, "Start Enemy Scanner")
                        scanner_started = True
                    
                    # Follow combat path - add individual waypoints so bot naturally fights enemies
                    if combat_path:
                        ConsoleLog("Bot", f"Adding combat path {idx + 1} with {len(combat_path)} waypoints")
                        for wp_idx, (x, y) in enumerate(combat_path):
                            _add_move_xy(x, y, f"Path {idx + 1} waypoint {wp_idx + 1}")
                            state_name = bot.config.FSM.states[-1].name
                            bot_vars.waypoint_states.append((state_name, float(x), float(y)))
            
            # Format 2: Simple list of coordinates (e.g., Norrhart_Domains)
            elif isinstance(first_element, (tuple, list)):
                ConsoleLog("Bot", f"Direct combat path with {len(bot_vars.bless_path_data)} waypoints")
                
                # No blessings in this format - start scanner immediately
                bot.States.AddCustomState(start_scanner, "Start Enemy Scanner")
                scanner_started = True
                
                for wp_idx, (x, y) in enumerate(bot_vars.bless_path_data):
                    _add_move_xy(x, y, f"Waypoint {wp_idx + 1}")
                    state_name = bot.config.FSM.states[-1].name
                    bot_vars.waypoint_states.append((state_name, float(x), float(y)))
            
            else:
                ConsoleLog("Bot", f"WARNING: Unknown path data format: {type(first_element)}", message_type=6)
    else:
        ConsoleLog("Bot", "WARNING: No blessing/path data defined!", message_type=6)
    
    # Fallback: If scanner wasn't started (edge case), start it now
    if not scanner_started:
        bot.States.AddCustomState(start_scanner, "Start Enemy Scanner")
        scanner_started = True

    # Terminal state: keep FSM alive (and scanner ticking) until vanquish is complete
    def _wait_vanquish_complete_fn():
        ConsoleLog("Bot", "Path complete - waiting for vanquish to finish...")
        while not Map.IsVanquishComplete():
            killed = Map.GetFoesKilled()
            total = Map.GetFoesToKill()
            ConsoleLog("Bot", f"Vanquish progress: {killed}/{total} foes killed")
            yield from Routines.Yield.wait(10000)
        ConsoleLog("Bot", "Vanquish complete!", message_type=1)

    bot.States.AddCustomState(_wait_vanquish_complete_fn, "Wait for Vanquish Complete")

# ============================================================================
# Death Handling
# ============================================================================

def _on_death(bot: Botting):
    """Coroutine to handle death recovery - wait for respawn then hand control back."""
    ConsoleLog("Death", "Waiting for respawn at resurrection shrine...")

    from Py4GWCoreLib import Agent, Player

    # Wait while dead
    while Agent.IsDead(Player.GetAgentID()):
        yield from Routines.Yield.wait(1000)

    ConsoleLog("Death", "Player respawned.")
    yield from Routines.Yield.wait(2000)

    # If the enemy scanner owns the FSM, wait until the scanner finishes its
    # navigate+reset+resume sequence and clears the flag. Do NOT touch the FSM
    # at all while the scanner is in control — it will call fsm.resume() itself
    # once the player is alive and navigation is complete.
    if _scanner_has_fsm_control[0]:
        ConsoleLog("Death", "Scanner is in control - waiting for scanner to resume FSM...")
        while _scanner_has_fsm_control[0]:
            yield from Routines.Yield.wait(500)
        ConsoleLog("Death", "Scanner released FSM control.")
        yield
        return

    # No scanner involvement: just resume so FollowPath continues from its paused state.
    # Do NOT call reset()+enter() - the paused coroutine is still alive and valid.
    fsm = bot.config.FSM
    fsm.resume()
    yield


def on_death(bot: Botting):
    """Death callback - pauses bot and initiates recovery"""
    ConsoleLog("Death", "Player died! Waiting for respawn...", message_type=6)
    
    # Pause FSM and clear action queues
    from Py4GWCoreLib import ActionQueueManager
    ActionQueueManager().ResetAllQueues()
    
    fsm = bot.config.FSM
    fsm.pause()
    
    # Add death recovery coroutine
    fsm.AddManagedCoroutine("OnDeath", _on_death(bot))

# ============================================================================
# Enemy Scanning & Combat Management
# ============================================================================

# Scanner configuration constants
SCANNER_AGGRO_RANGE = 1200  # Distance to approach enemies before they aggro
SCANNER_MOVEMENT_TIMEOUT = 10000  # Max time to move to enemy (10s)
SCANNER_ENGAGE_TIMEOUT = 8000  # Max time to wait for party engagement (8s)
SCANNER_AGGRO_DETECTION_TIMEOUT = 10000  # Max time to wait for enemies to aggro (10s)
SCANNER_CLEAR_CONFIRMATION_TIME = 4000  # Time to confirm group cleared (4s)
SCANNER_COMBAT_CLEAR_CONFIRMATION_TIME = 3000  # Time to confirm FSM combat finished (3s)
SCANNER_RESCAN_DELAY = 5000  # Delay between scans (5s)
SCANNER_POST_GROUP_DELAY = 5000  # Delay after clearing group (5s for loot)
SCANNER_POST_COMBAT_DELAY = 3000  # Delay after FSM combat finishes (3s)
SCANNER_PULL_RETRY_COUNT = 3  # Direct pull attempts if party doesn't auto-engage


def _get_enemies_in_range(position, range_value=Range.Compass.value, alive_only=True, aggressive_only=False):
    """
    Get filtered enemy array within specified range.
    
    Args:
        position: Player position (x, y) tuple
        range_value: Maximum distance to filter enemies (default: Compass range)
        alive_only: Filter for alive enemies only (default: True)
        aggressive_only: Filter for aggressive enemies only (default: False)
    
    Returns:
        List of enemy agent IDs matching the filters
    """
    enemies = AgentArray.GetEnemyArray()
    enemies = AgentArray.Filter.ByDistance(enemies, position, range_value)
    
    if alive_only:
        enemies = AgentArray.Filter.ByCondition(enemies, lambda a: Agent.IsAlive(a))
    
    if aggressive_only:
        enemies = [e for e in enemies if Agent.IsAggressive(e)]
    
    return enemies


def _wait_for_combat_clear(confirmation_time):
    """
    Coroutine to wait until no aggressive enemies remain in compass range.
    Uses confirmation timer to ensure combat is truly finished before continuing.
    
    Args:
        confirmation_time: Duration in milliseconds to confirm no enemies remain
    """
    clear_timer = Timer()
    clear_started = False
    
    while True:
        yield from Routines.Yield.wait(1000)
        
        player_pos = Player.GetXY()
        aggressive_enemies = _get_enemies_in_range(player_pos, aggressive_only=True)
        
        if len(aggressive_enemies) == 0:
            if not clear_started:
                clear_timer.Start()
                clear_started = True
            
            if clear_timer.HasElapsed(confirmation_time):
                break
        else:
            clear_started = False


def _enemy_scanner_coroutine(bot: Botting):
    """
    Background coroutine that scans for nearby enemies and ensures combat engagement.
    Helps achieve 100% vanquish by detecting enemies that might be skipped.
    """
    
    while True:
        # Only scan when in explorable area
        if not Routines.Checks.Map.IsExplorable():
            yield from Routines.Yield.wait(1000)
            continue

        # Detect stale FSM pause: _coro_follow_path_to() calls fsm.pause() when it
        # detects a party wipe during movement (after scanner already released control).
        # If the FSM is paused but the scanner doesn't own it AND the player is alive,
        # nothing will ever resume it — the scanner must rescue the FSM here.
        # Guard: if player is dead, _on_death handles the resume; don't interfere.
        fsm = bot.config.FSM
        if fsm.is_paused() and not _scanner_has_fsm_control[0] and not Agent.IsDead(Player.GetAgentID()):
            ConsoleLog("Scanner", "FSM is paused without scanner control - rescuing stale pause, resuming.")
            # Do NOT reset the current state. The movement coroutine called fsm.pause()
            # then finished (StopIteration). Resetting would restart it into the same
            # party-wipe infinite loop. Just resume so can_exit() fires naturally.
            _scanner_resume_suppress_state[0] = fsm.current_state.name if fsm.current_state else None
            _scanner_resume_suppress_ts[0] = Utils.GetBaseTimestamp()
            fsm.resume()
            yield from Routines.Yield.wait(1000)
            continue

        # Suppress new scans after a resume until the player reaches the current
        # waypoint (FSM state changes) or the safety-net timeout fires.
        # Without this, the scanner immediately re-pauses the FSM while the player
        # is still walking back from the combat area, creating an infinite loop.
        if _scanner_resume_suppress_state[0] is not None:
            current_name = fsm.current_state.name if fsm.current_state else None
            elapsed = Utils.GetBaseTimestamp() - _scanner_resume_suppress_ts[0]

            # ── Deadlock rescue ──────────────────────────────────────────────────
            # _coro_follow_path_to() can call fsm.pause() any time it detects a
            # party wipe — including AFTER the scanner has already resumed the FSM.
            # If that happens during the suppress window the FSM is paused with no
            # owner and nothing will ever unfreeze it, so we must rescue it here.
            if fsm.is_paused() and not _scanner_has_fsm_control[0] and not Agent.IsDead(Player.GetAgentID()):
                ConsoleLog("Scanner", f"FSM re-paused during suppress window (state '{current_name}') - rescuing deadlock.")
                # Do NOT reset the current state. The movement coroutine already
                # finished (called fsm.pause() then returned). Resetting would restart
                # it into the same party-wipe freeze loop. Just resume so that
                # can_exit() fires naturally and the FSM advances to the next state.
                fsm.resume()
                yield from Routines.Yield.wait(1000)
                continue

            # ── Periodic status log so we can diagnose hangs ─────────────────────
            if elapsed > 0 and (elapsed // 30000) != ((elapsed - 1000) // 30000):
                player_pos_log = Player.GetXY()
                dist_log = 0.0
                for (sname, wx, wy) in bot_vars.waypoint_states:
                    if sname == _scanner_resume_suppress_state[0]:
                        dist_log = Utils.Distance(player_pos_log, (wx, wy))
                        break
                ConsoleLog("Scanner", f"[Suppress] FSM='{current_name}' paused={fsm.is_paused()} dist_to_wp={dist_log:.0f} elapsed={elapsed//1000}s")

            if current_name != _scanner_resume_suppress_state[0]:
                # FSM advanced past the suppressed state — player reached the waypoint.
                ConsoleLog("Scanner", f"FSM advanced to '{current_name}' - re-enabling enemy scanning.")
                _scanner_resume_suppress_state[0] = None
            elif elapsed >= SCANNER_RESUME_SUPPRESS_TIMEOUT_MS:
                ConsoleLog("Scanner", "Resume suppress timeout reached - re-enabling enemy scanning.")
                _scanner_resume_suppress_state[0] = None
            else:
                # Check if player has physically arrived within range of the target waypoint.
                # This fires before the state-name change and is the normal early-clear path:
                # once the player is close enough to the waypoint, re-enable scanning.
                suppress_cleared = False
                for (sname, wx, wy) in bot_vars.waypoint_states:
                    if sname == _scanner_resume_suppress_state[0]:
                        dist_to_wp = Utils.Distance(Player.GetXY(), (wx, wy))
                        if dist_to_wp <= SCANNER_RESUME_CLEAR_DISTANCE:
                            ConsoleLog("Scanner", f"Player within {dist_to_wp:.0f} units of target waypoint '{sname}' - re-enabling enemy scanning.")
                            _scanner_resume_suppress_state[0] = None
                            suppress_cleared = True
                        break
                if not suppress_cleared:
                    yield from Routines.Yield.wait(1000)
                    continue

        # Get all enemies in compass range
        player_pos = Player.GetXY()
        all_enemies = _get_enemies_in_range(player_pos)
        
        if len(all_enemies) == 0:
            yield from Routines.Yield.wait(SCANNER_RESCAN_DELAY)
            continue
        
        # Separate unaggred and aggressive enemies
        unaggred_enemies = [e for e in all_enemies if not Agent.IsAggressive(e)]
        aggressive_enemies = [e for e in all_enemies if Agent.IsAggressive(e)]
        
        if len(unaggred_enemies) == 0:
            yield from Routines.Yield.wait(SCANNER_RESCAN_DELAY)
            continue
        
        # If party is already fighting, wait for combat to finish first
        if len(aggressive_enemies) > 0:
            ConsoleLog("Scanner", f"Found {len(unaggred_enemies)} unaggred enemies but party is fighting {len(aggressive_enemies)} enemies - waiting...")
            yield from _wait_for_combat_clear(SCANNER_COMBAT_CLEAR_CONFIRMATION_TIME)
            ConsoleLog("Scanner", "Current combat finished, now scanning for missed enemies...")
            yield from Routines.Yield.wait(SCANNER_POST_COMBAT_DELAY)
            continue
        
        # No active combat - engage unaggred enemies
        ConsoleLog("Scanner", f"Detected {len(unaggred_enemies)} unaggred enemies, engaging all groups...")
        
        # Pause FSM so its waypoint Move calls don't override scanner movement
        fsm.pause()
        _scanner_has_fsm_control[0] = True

        # Breadcrumb: record the exact position the player was at when the scanner
        # took over. The FollowPath coroutine (alive in managed_coroutines, paused
        # via fsm_pause() check) was computing nodes FROM this position. Returning
        # here before resume means FollowPath can simply continue its existing path.
        breadcrumb_pos = Player.GetXY()
        
        # Engage each group until all unaggred enemies are cleared
        consecutive_skips = 0  # track how many targets in a row were skipped without engaging
        while True:
            # Refresh enemy list
            player_pos = Player.GetXY()
            unaggred_enemies = [e for e in _get_enemies_in_range(player_pos) if not Agent.IsAggressive(e)]
            
            if len(unaggred_enemies) == 0:
                ConsoleLog("Scanner", "All enemy groups engaged")
                break

            # If we've skipped too many targets in a row the heroes are already
            # dealing with them from range - wait for the current fight to finish
            # rather than chasing targets endlessly.
            if consecutive_skips >= 4:
                ConsoleLog("Scanner", "Too many consecutive skips - waiting for current combat to settle...")
                yield from _wait_for_combat_clear(SCANNER_CLEAR_CONFIRMATION_TIME)
                consecutive_skips = 0
                continue
            
            # Target nearest unaggred enemy
            unaggred_enemies = AgentArray.Sort.ByDistance(unaggred_enemies, player_pos)
            nearest_enemy = unaggred_enemies[0]
            
            ConsoleLog("Scanner", f"Found {len(unaggred_enemies)} unaggred enemies remaining in range")

            # Don't chase a new target while enemies are actively attacking the
            # player (within earshot). Heroes fighting at range in compass distance
            # does NOT count - this only fires if something is in the player's face.
            if _get_enemies_in_range(Player.GetXY(), Range.Earshot.value, aggressive_only=True):
                ConsoleLog("Scanner", "Enemies attacking player - waiting for combat to clear before moving to next group...")
                yield from _wait_for_combat_clear(SCANNER_CLEAR_CONFIRMATION_TIME)
                consecutive_skips = 0
                continue
            
            Player.ChangeTarget(nearest_enemy)
            yield from Routines.Yield.wait(500)
            
            # Move to enemy
            enemy_pos = Agent.GetXY(nearest_enemy)
            if not enemy_pos or (abs(enemy_pos[0]) < 1 and abs(enemy_pos[1]) < 1):
                ConsoleLog("Scanner", "Skipping target with invalid position data")
                yield from Routines.Yield.wait(500)
                consecutive_skips += 1
                continue
            
            ConsoleLog("Scanner", f"Moving to enemy group at ({enemy_pos[0]:.0f}, {enemy_pos[1]:.0f})")
            
            movement_timer = Timer()
            movement_timer.Start()
            Player.Move(enemy_pos[0], enemy_pos[1])
            
            # Wait until within aggro range or timeout
            skipped = False
            while not movement_timer.HasElapsed(SCANNER_MOVEMENT_TIMEOUT):
                if Utils.Distance(Player.GetXY(), enemy_pos) < SCANNER_AGGRO_RANGE:
                    break
                
                # Skip if enemy died or was engaged by party
                if not Agent.IsAlive(nearest_enemy) or Agent.IsAggressive(nearest_enemy):
                    ConsoleLog("Scanner", "Enemy died or was engaged by party, moving to next group...")
                    yield from Routines.Yield.wait(1000)
                    skipped = True
                    break
                
                yield from Routines.Yield.wait(500)
            else:
                # Movement timeout - skip this enemy
                consecutive_skips += 1
                continue

            # Skip if enemy was already killed/engaged
            if skipped or not Agent.IsAlive(nearest_enemy) or Agent.IsAggressive(nearest_enemy):
                consecutive_skips += 1
                yield from Routines.Yield.wait(500)
                continue
            
            consecutive_skips = 0
            
            # Wait for party to engage
            ConsoleLog("Scanner", "Reached enemy position, waiting for party to engage...")
            
            engage_timer = Timer()
            engage_timer.Start()
            while not Routines.Checks.Agents.InDanger(Range.Compass) and not engage_timer.HasElapsed(SCANNER_ENGAGE_TIMEOUT):
                yield from Routines.Yield.wait(500)
            
            if not Routines.Checks.Agents.InDanger(Range.Compass):
                ConsoleLog("Scanner", "Party didn't engage, forcing pull on target...")

                pull_succeeded = False
                for pull_attempt in range(SCANNER_PULL_RETRY_COUNT):
                    if not Agent.IsAlive(nearest_enemy):
                        pull_succeeded = True
                        break

                    Player.ChangeTarget(nearest_enemy)
                    yield from Routines.Yield.wait(200)
                    Player.Interact(nearest_enemy, False)
                    yield from Routines.Yield.wait(900)

                    if Routines.Checks.Agents.InDanger(Range.Compass) or Agent.IsAggressive(nearest_enemy):
                        pull_succeeded = True
                        ConsoleLog("Scanner", f"Pull succeeded on attempt {pull_attempt + 1}")
                        break

                if not pull_succeeded:
                    ConsoleLog("Scanner", "Enemies still didn't aggro after pull attempts, moving to next group...")
                    continue
            
            # Combat started - wait for enemies to aggro
            ConsoleLog("Scanner", "Combat started, waiting for enemies to aggro...")
            
            aggro_timer = Timer()
            aggro_timer.Start()
            enemies_aggroed = False
            
            while not enemies_aggroed and not aggro_timer.HasElapsed(SCANNER_AGGRO_DETECTION_TIMEOUT):
                yield from Routines.Yield.wait(500)
                
                aggressive_count = len(_get_enemies_in_range(Player.GetXY(), aggressive_only=True))
                if aggressive_count > 0:
                    enemies_aggroed = True
                    ConsoleLog("Scanner", f"Enemies aggroed ({aggressive_count} aggressive), waiting for group to be cleared...")
            
            if not enemies_aggroed:
                ConsoleLog("Scanner", "Enemies didn't aggro, moving to next group...")
                continue
            
            # Wait for group to be cleared with confirmation
            clear_timer = Timer()
            clear_started = False
            
            while True:
                yield from Routines.Yield.wait(1000)
                
                aggressive_enemies = _get_enemies_in_range(Player.GetXY(), aggressive_only=True)
                
                if len(aggressive_enemies) == 0:
                    if not clear_started:
                        clear_timer.Start()
                        clear_started = True
                        ConsoleLog("Scanner", "No aggressive enemies detected, confirming clear...")
                    
                    if clear_timer.HasElapsed(SCANNER_CLEAR_CONFIRMATION_TIME):
                        ConsoleLog("Scanner", "Group cleared (confirmed)")
                        break
                else:
                    if clear_started:
                        ConsoleLog("Scanner", f"Combat continues ({len(aggressive_enemies)} enemies still aggressive)")
                    clear_started = False
            
            # Wait before checking for next group
            yield from Routines.Yield.wait(SCANNER_POST_GROUP_DELAY)
            ConsoleLog("Scanner", "Checking for more enemies...")
        
        # After scanner combat: find the nearest upcoming waypoint, jump to it, reset,
        # and resume. This handles the case where the scanner chased enemies far from
        # the current FSM waypoint — instead of sending the player on a 20000-unit
        # backtrack, we jump to whichever waypoint they're already closest to.

        # Wait for the player to be alive before resuming (party wipe during combat).
        dead_wait_logged = False
        while Agent.IsDead(Player.GetAgentID()):
            if not dead_wait_logged:
                ConsoleLog("Scanner", "Player is dead - waiting for respawn before resuming path...")
                dead_wait_logged = True
            yield from Routines.Yield.wait(1000)
        if dead_wait_logged:
            yield from Routines.Yield.wait(2000)  # brief settle after respawn

        # Find the nearest upcoming waypoint state and jump to it so the player
        # doesn't have to walk back across the map to a now-distant waypoint target.
        # bot_vars.waypoint_states is built at FSM construction time: list[(name, x, y)].
        player_pos_now = Player.GetXY()
        current_fsm_index = fsm.states.index(fsm.current_state) if fsm.current_state and fsm.current_state in fsm.states else 0

        # Find the correct waypoint to resume from.
        # Strategy: scan forward through waypoint_states (in path order, from current
        # FSM index onward) and pick the FIRST waypoint the player hasn't reached yet
        # (distance > movement tolerance). This avoids picking a waypoint the player
        # just walked past (which would send them backwards) while still skipping
        # unreachably-distant waypoints that the scanner's combat happened to be near.
        MOVE_TOLERANCE = 200  # slightly above FollowPath's default 150-unit tolerance

        # Build ordered list of upcoming (state_name, x, y, fsm_index) tuples
        upcoming: list[tuple[str, float, float, int]] = []
        for (sname, wx, wy) in bot_vars.waypoint_states:
            try:
                sidx = next(i for i, s in enumerate(fsm.states) if s.name == sname)
            except StopIteration:
                continue
            if sidx >= current_fsm_index:
                upcoming.append((sname, wx, wy, sidx))

        # Sort by FSM index to ensure path order
        upcoming.sort(key=lambda t: t[3])

        target_state_name: str | None = None
        if upcoming:
            # First waypoint that hasn't been reached yet
            for (sname, wx, wy, _sidx) in upcoming:
                if Utils.Distance(player_pos_now, (wx, wy)) > MOVE_TOLERANCE:
                    target_state_name = sname
                    break
            # If player is inside tolerance of ALL upcoming waypoints (unlikely but
            # possible at very end of path), pick the last one so FSM can finish.
            if target_state_name is None:
                target_state_name = upcoming[-1][0]

        target_dist = 0.0
        if target_state_name:
            for (sname, wx, wy, _) in upcoming:
                if sname == target_state_name:
                    target_dist = Utils.Distance(player_pos_now, (wx, wy))
                    break

        if target_state_name and target_state_name != (fsm.current_state.name if fsm.current_state else None):
            ConsoleLog("Scanner", f"Jumping to next unreached waypoint state '{target_state_name}' ({target_dist:.0f} units away).")
            try:
                fsm.jump_to_state_by_name(target_state_name)
            except ValueError as e:
                ConsoleLog("Scanner", f"Jump failed: {e} - resetting current state instead.")
                if fsm.current_state:
                    fsm.current_state.reset()
        else:
            # Already at the correct state, just reset so it recomputes AutoPath
            if fsm.current_state:
                ConsoleLog("Scanner", f"Resetting state '{fsm.current_state.name}' ({target_dist:.0f} units away) for fresh AutoPath.")
                fsm.current_state.reset()

        # Record the current FSM state name for suppress-after-resume.
        _scanner_resume_suppress_state[0] = fsm.current_state.name if fsm.current_state else None
        _scanner_resume_suppress_ts[0] = Utils.GetBaseTimestamp()
        _scanner_has_fsm_control[0] = False
        ConsoleLog("Scanner", f"All enemies in range cleared, resuming main path (suppressing scans until FSM advances from '{_scanner_resume_suppress_state[0]}')...")
        fsm.resume()
        
        # Main scan delay before next sweep
        yield from Routines.Yield.wait(SCANNER_RESCAN_DELAY)

# ============================================================================
# Map Discovery Functions
# ============================================================================

def get_available_regions():
    """Scan MAPS_DIR and return list of region folders"""
    regions = []
    try:
        if os.path.exists(MAPS_DIR):
            for item in os.listdir(MAPS_DIR):
                item_path = os.path.join(MAPS_DIR, item)
                if os.path.isdir(item_path) and not item.startswith('.'):
                    regions.append(item)
        regions.sort()
    except Exception as e:
        ConsoleLog("MapScanner", f"Error scanning regions: {e}")
    return regions


def get_available_maps(region):
    """Scan region folder and return list of available map files"""
    maps = []
    try:
        region_path = os.path.join(MAPS_DIR, region)
        if os.path.exists(region_path):
            for item in os.listdir(region_path):
                if item.endswith('.py') and not item.startswith('__'):
                    map_name = item[:-3]  # Remove .py extension
                    maps.append(map_name)
        maps.sort()
    except Exception as e:
        ConsoleLog("MapScanner", f"Error scanning maps for {region}: {e}")
    return maps


def load_basic_map_data(region, map_name):
    """
    Parse map .py file to extract all map data: IDs, paths, and blessing locations.
    Updates bot_vars with the extracted data for FSM initialization.
    """
    try:
        map_file_path = os.path.join(MAPS_DIR, region, f"{map_name}.py")
        
        if not os.path.exists(map_file_path):
            ConsoleLog("MapLoader", f"Map file not found: {map_file_path}")
            return False
            
        ConsoleLog("MapLoader", f"Loading: {region}/{map_name}")
        
        # Execute the map file to load its data
        import sys
        map_module_globals = {}
        with open(map_file_path, 'r', encoding='utf-8') as f:
            exec(f.read(), map_module_globals)
        
        # Extract IDs
        ids_var_name = f"{map_name}_ids"
        if ids_var_name not in map_module_globals:
            ConsoleLog("MapLoader", f"Could not find {ids_var_name} in file")
            return False
        
        ids_dict = map_module_globals[ids_var_name]
        outpost_id = ids_dict.get('outpost_id', 0)
        map_id = ids_dict.get('map_id', 0)
        
        if outpost_id == 0:
            ConsoleLog("MapLoader", "Warning: Outpost ID is 0")
            return False
        
        # Extract outpost path
        outpost_path_var = f"{map_name}_outpost_path"
        outpost_path = map_module_globals.get(outpost_path_var, [])
        
        if not outpost_path:
            ConsoleLog("MapLoader", f"Warning: No outpost path found ({outpost_path_var})")
        
        # Extract blessing and combat path data
        bless_path_var = map_name
        bless_path_data = map_module_globals.get(bless_path_var, [])
        
        if not bless_path_data:
            ConsoleLog("MapLoader", f"Warning: No blessing/path data found ({bless_path_var})")
        
        # Update bot variables
        bot_vars.current_outpost_id = outpost_id
        bot_vars.current_map_id = map_id
        bot_vars.outpost_path = outpost_path
        bot_vars.bless_path_data = bless_path_data
        
        ConsoleLog("MapLoader", f"Loaded - Outpost: {outpost_id}, Map: {map_id}")
        ConsoleLog("MapLoader", f"  Outpost path waypoints: {len(outpost_path)}")
        
        # Determine path data format
        if bless_path_data and len(bless_path_data) > 0:
            if isinstance(bless_path_data[0], dict):
                ConsoleLog("MapLoader", f"  Blessing locations: {len(bless_path_data)}")
            elif isinstance(bless_path_data[0], (tuple, list)):
                ConsoleLog("MapLoader", f"  Combat path waypoints: {len(bless_path_data)} (no blessings)")
        
        return True
            
    except Exception as e:
        ConsoleLog("MapLoader", f"Error loading map data: {e}")
        import traceback
        ConsoleLog("MapLoader", traceback.format_exc())
        return False

# ============================================================================
# Settings Tab UI
# ============================================================================

def _draw_settings():
    """Custom Settings tab UI for region/map selection"""
    global routine_set, needs_routine_init
    
    PyImGui.text("Map Selection")
    PyImGui.separator()

    # HeroAI toggle
    new_use_hero_ai = PyImGui.checkbox("Enable HeroAI", bot_vars.use_hero_ai)
    if new_use_hero_ai != bot_vars.use_hero_ai:
        bot_vars.use_hero_ai = new_use_hero_ai
        if routine_set:
            routine_set = False
            needs_routine_init = True
        ConsoleLog("UI", f"HeroAI {'enabled' if bot_vars.use_hero_ai else 'disabled'} - routine will rebuild")

    # Enemy scanner toggle
    new_use_enemy_scanner = PyImGui.checkbox("Enable Enemy Scanner", bot_vars.use_enemy_scanner)
    if new_use_enemy_scanner != bot_vars.use_enemy_scanner:
        bot_vars.use_enemy_scanner = new_use_enemy_scanner
        if routine_set:
            routine_set = False
            needs_routine_init = True
        ConsoleLog("UI", f"Enemy scanner {'enabled' if bot_vars.use_enemy_scanner else 'disabled'} - routine will rebuild")
    PyImGui.separator()
    
    # Region dropdown
    regions = get_available_regions()
    if regions:
        current_region_index = regions.index(bot_vars.selected_region) if bot_vars.selected_region in regions else 0
        selected_index = PyImGui.combo("Region", current_region_index, regions)
        
        if selected_index != current_region_index and selected_index < len(regions):
            bot_vars.selected_region = regions[selected_index]
            bot_vars.selected_map = ""  # Reset map when region changes
            bot_vars.current_outpost_id = 0
            bot_vars.current_map_id = 0
            ConsoleLog("UI", f"Region selected: {bot_vars.selected_region}")
    else:
        PyImGui.text_colored("No regions found", (1, 0, 0, 1))
    
    # Map dropdown
    if bot_vars.selected_region:
        maps = get_available_maps(bot_vars.selected_region)
        if maps:
            current_map_index = maps.index(bot_vars.selected_map) if bot_vars.selected_map in maps else 0
            selected_index = PyImGui.combo("Map", current_map_index, maps)
            
            if selected_index != current_map_index and selected_index < len(maps):
                bot_vars.selected_map = maps[selected_index]
                ConsoleLog("UI", f"Map selected: {bot_vars.selected_map}")
                
                # Load map data and flag for FSM rebuild
                if load_basic_map_data(bot_vars.selected_region, bot_vars.selected_map):
                    routine_set = False
                    needs_routine_init = True
        else:
            PyImGui.text("No maps found for region")
    else:
        PyImGui.text("Select a region first")
    
    PyImGui.separator()
    
    # Display current selection status
    if bot_vars.selected_region and bot_vars.selected_map:
        PyImGui.text_colored(f"Selected: {bot_vars.selected_region}/{bot_vars.selected_map}", (0, 1, 0, 1))
        
        if bot_vars.current_outpost_id > 0:
            PyImGui.text(f"Outpost ID: {bot_vars.current_outpost_id}")
            PyImGui.text(f"Map ID: {bot_vars.current_map_id}")
            
            if routine_set:
                PyImGui.text_colored("Ready to start!", (0, 1, 0, 1))
            elif needs_routine_init:
                PyImGui.text_colored("Initializing...", (1, 1, 0, 1))
        else:
            PyImGui.text_colored("Warning: Map data not loaded", (1, 1, 0, 1))
    else:
        PyImGui.text_colored("Please select a region and map", (1, 1, 0, 1))


bot.UI.override_draw_config(lambda: _draw_settings())

# ============================================================================
# Core Routine Loop
# ============================================================================

def main():
    """Main update loop - handles FSM initialization and bot updates"""
    global routine_set, needs_routine_init
    
    # Initialize/rebuild FSM when needed (outside ImGui context)
    if needs_routine_init and not routine_set:
        ConsoleLog("UI", f"Initializing routine for {bot_vars.selected_region}/{bot_vars.selected_map}...")
        try:
            # Clear old FSM states if switching maps
            if hasattr(bot.config.FSM, 'states') and bot.config.FSM.states:
                ConsoleLog("UI", "Clearing old FSM states...")
                bot.config.FSM.states.clear()
                bot.config.FSM.current_state = None
            
            # CRITICAL: Reset initialized flag to trigger FSM rebuild
            # bot.Update() will call self.Routine() when config.initialized == False
            bot.config.initialized = False
            bot.SetMainRoutine(bot_routine)
            
            routine_set = True
            needs_routine_init = False
            ConsoleLog("UI", "Bot ready! Start from the Main tab.")
            
        except Exception as e:
            ConsoleLog("UI", f"ERROR during initialization: {e}", message_type=6)
            import traceback
            ConsoleLog("UI", traceback.format_exc(), message_type=6)
            routine_set = False
            needs_routine_init = False
   
    # Prevent bot from starting without map selection
    if not routine_set:
        # If the UI start button was clicked with no FSM states, stop immediately
        if bot.config.fsm_running:
            ConsoleLog("UI", "ERROR: Select a map before starting!", message_type=6)
            bot.Stop()
    
    # Standard bot update
    bot.Update()
    try:
        bot.UI.draw_window()
    except ValueError as e:
        # Guard against FSM.restart() crashing when no states exist yet
        # (user clicked Start before selecting a map)
        if "No states have been added" in str(e):
            ConsoleLog("UI", "ERROR: Select a map before starting!", message_type=6)
            bot.Stop()
        else:
            raise


if __name__ == "__main__":
    main()