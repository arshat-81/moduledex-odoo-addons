/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, onMounted, onWillUnmount } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

/**
 * Invisible widget. Attach it to a plain, non-stored dummy field
 * (widget="ai_migrator_auto_refresh") on a form view and it will
 * periodically call record.load() so the form pulls fresh data from
 * the server without the user having to hit refresh.
 *
 * Options:
 *  - interval: seconds between polls (default 8)
 *
 * Deliberately unconditional: earlier versions of this widget skipped
 * polling once the record's "state" field was in a configurable
 * "stop_states" list, to save a bit of background traffic once a job
 * looked finished. That gate read record.data.state *before* ever
 * reloading it — so if the state visible at mount time was already
 * stale (e.g. right after a button click writes a new state and queues
 * a background job, but the click's own response doesn't force a form
 * reload — which is the common case for buttons here, since they're
 * usually clicked from a state that WAS in stop_states, like
 * re-generating from "generated" or retrying from "failed"), the widget
 * would conclude nothing was running and would never poll again for the
 * lifetime of the mount — a permanent deadlock with no way to recover
 * short of a manual page refresh. Polling unconditionally trades a
 * negligible amount of background read traffic (this is a manually
 * opened admin form, not a high-traffic page) for guaranteed liveness.
 */
export class AiMigratorAutoRefresh extends Component {
    static template = "ai_module_migrator.AutoRefreshWidget";
    static props = {
        ...standardFieldProps,
        interval: { type: Number, optional: true },
    };

    setup() {
        this.timer = null;
        onMounted(() => this._start());
        onWillUnmount(() => this._stop());
    }

    _start() {
        const seconds = this.props.interval || 8;
        this._stop();
        this.timer = setInterval(() => {
            // record.load() is async — a rejected promise from a transient
            // network/session failure must be caught here, not with a
            // synchronous try/catch, or it surfaces as an unhandled
            // rejection (Odoo's web client shows that as an error dialog).
            this.props.record.load().catch(() => {
                // Never let a failed poll break the form; just skip this tick.
            });
        }, Math.max(3, seconds) * 1000);
    }

    _stop() {
        if (this.timer) {
            clearInterval(this.timer);
            this.timer = null;
        }
    }
}

registry.category("fields").add("ai_migrator_auto_refresh", {
    component: AiMigratorAutoRefresh,
    supportedTypes: ["char", "boolean", "integer"],
    extractProps: ({ options }) => ({
        interval: options && options.interval,
    }),
});
