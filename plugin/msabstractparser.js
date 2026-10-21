// Feature toggles (flip these to experiment locally; rebuild elephant.fda after changing).
var EMBED_AUTO_CAPTIONS = true;   // merge yt-dlp automatic_captions into subtitles (tagged "(auto)")
var PASS_EXTRA_PROPERTIES = true;  // pass rich YouTube metadata (description, channel, tags, chapters, ...) through
var INCLUDE_CHAPTERS = true;      // include the chapters array in the parse result

var g_browsers = Object.create(null);

var msAbstractParser = (function()
{
    function MsAbstractParser()
    {
    }

    MsAbstractParser.prototype = {

        parse: function (obj, customArgs)
        {
            console.log("parsing...");

            let args = [];
            let tmpCookies;
            let systemUserAgent;
            let systemBrowser;
            let allowWbCookies = true;
            
            try
            {
                systemUserAgent = qtJsSystem.defaultUserAgent;
                systemBrowser = qtJsSystem.defaultWebBrowser;
                allowWbCookies = App.pluginsAllowWbCookies;
            }
            catch(e) {}

            let proxyUrl = qtJsNetworkProxyMgr.proxyForUrl(obj.url).url();
            if (proxyUrl)
            {
                proxyUrl = proxyUrl.replace(/^https:\/\//i, 'http://'); // FDM bug workaround
                args.push("--proxy", proxyUrl);
            }

            args.push("-J", "--flat-playlist", "--no-warnings", "--compat-options", "no-youtube-unavailable-videos");

            if (allowWbCookies)
            {
                if (obj.cookies && obj.cookies.length)
                {
                    tmpCookies = qtJsTools.createTmpFile("request_" + obj.requestId + "_cookies");
                    if (tmpCookies && tmpCookies.writeText(cookiesToNetscapeText(obj.cookies)))
                        args.push("--cookies", tmpCookies.path);
                }
                else
                {
                    let browser = obj.browser || systemBrowser;
                    if (browser)
                    {
                        if (!(browser in g_browsers))
                        {
                            return this.checkBrowser(obj.requestId, obj.interactive, browser)
                                .then(() => this.parse(obj, customArgs));
                        }
                        else
                        {
                            // In case the current web browser is not supported,
                            // let's try Mozilla Firefox as the most well-supported web browser.
                            if (!g_browsers[browser] && browser !== "firefox")
                            {
                                browser = "firefox";
                                
                                if (!(browser in g_browsers))
                                {
                                    return this.checkBrowser(obj.requestId, obj.interactive, browser)
                                        .then(() => this.parse(obj, customArgs));
                                }
                            }
                            
                            if (g_browsers[browser])
                                args.push('--cookies-from-browser', browser);
                        }
                    }
                }
            }
            
            let userAgent = obj.userAgent || systemUserAgent;
            if (userAgent)
                args.push('--user-agent', userAgent);

            if (customArgs.length)
                args = args.concat(customArgs);

            args.push(obj.url);

            let sourceName = /^https?:\/\/(?:[a-z0-9-]+\.)*(?:youtube\.com|youtu\.be|youtube-nocookie\.com)(?::\d+)?(?:[/?#]|$)/i.test(obj.url) ? "YouTube" : "Web site";

            return launchPythonScript(obj.requestId, obj.interactive, "yt-dlp/elephant_launcher.py", args)
            .then(function(obj)
            {
                logPythonResult(obj);

                return new Promise(function (resolve, reject)
                {
                    let output = obj.output.trim();
                    if (!output || output[0] !== '{')
                    {
                        let isUnsupportedUrl = /ERROR:\s*(\[generic\])?\s*Unsupported URL:/.test(obj.errorOutput);
                        let isBotCheck = !isUnsupportedUrl && /Sign in to confirm you['\u2019]re not a bot/.test(obj.errorOutput);
                        let errDesc = isUnsupportedUrl ? "Unsupported URL" :
                            isBotCheck ? "Elephant add-on: " + sourceName + " requires cookies to download this video. See <a href='https://github.com/meowcateatrat/elephant/#troubleshooting'>Troubleshooting ↗</a>." :
                            "Parse error";
                        reject({
                            error: errDesc,
                            isParseError: !isUnsupportedUrl && !isBotCheck,
                            isAdditionalActionsRequiredError: isBotCheck
                        });
                    }
                    else
                    {
                        let parsed;
                        try { parsed = JSON.parse(output); } catch (e) { parsed = null; }

                        // Surface automatic captions as selectable subtitles.
                        // FDM reads the "subtitles" field of yt-dlp's info JSON; auto-generated
                        // captions live under "automatic_captions", so merge them in (without
                        // clobbering any manual subtitle track for the same language).
                        if (EMBED_AUTO_CAPTIONS && parsed && parsed.automatic_captions)
                        {
                            if (!parsed.subtitles || typeof parsed.subtitles !== "object")
                                parsed.subtitles = {};

                            for (const lang in parsed.automatic_captions)
                            {
                                const existing = parsed.subtitles[lang];
                                const hasManual = Array.isArray(existing) && existing.length > 0;
                                if (!hasManual)
                                {
                                    parsed.subtitles[lang] = (parsed.automatic_captions[lang] || []).map(function (entry)
                                    {
                                        const copy = Object.assign({}, entry);
                                        if (!copy.name)
                                            copy.name = lang + " (auto)";
                                        else if (!/\(auto\)/i.test(copy.name))
                                            copy.name = copy.name + " (auto)";
                                        return copy;
                                    });
                                }
                            }
                        }

                        // Same treatment for playlist entries.
                        if (EMBED_AUTO_CAPTIONS && parsed && Array.isArray(parsed.entries))
                        {
                            for (let i = 0; i < parsed.entries.length; ++i)
                            {
                                const e = parsed.entries[i];
                                if (e && e.automatic_captions)
                                {
                                    if (!e.subtitles || typeof e.subtitles !== "object")
                                        e.subtitles = {};
                                    for (const lang in e.automatic_captions)
                                    {
                                        const existing = e.subtitles[lang];
                                        const hasManual = Array.isArray(existing) && existing.length > 0;
                                        if (!hasManual)
                                        {
                                            e.subtitles[lang] = (e.automatic_captions[lang] || []).map(function (entry)
                                            {
                                                const copy = Object.assign({}, entry);
                                                if (!copy.name)
                                                    copy.name = lang + " (auto)";
                                                else if (!/\(auto\)/i.test(copy.name))
                                                    copy.name = copy.name + " (auto)";
                                                return copy;
                                            });
                                        }
                                    }
                                }
                            }
                        }

                        // Pass rich YouTube metadata through as a "properties" object so FDM's
                        // Properties dialog (and any future field) can display it. Also keep
                        // the chapters array when enabled. These are best-effort: FDM only
                        // reads fields it knows, but extra keys never hurt.
                        if (parsed && PASS_EXTRA_PROPERTIES)
                        {
                            const extraKeys = ["description", "channel", "channel_id", "uploader",
                                "uploader_id", "uploader_url", "tags", "categories", "view_count",
                                "like_count", "comment_count", "age_limit", "duration_string",
                                "upload_date", "webpage_url", "original_url", "extractor",
                                "extractor_key", "release_year", "availability", "creators",
                                "creator", "channel_follower_count"];
                            const props = {};
                            for (let k = 0; k < extraKeys.length; ++k)
                            {
                                const key = extraKeys[k];
                                if (parsed[key] !== undefined && parsed[key] !== null)
                                    props[key] = parsed[key];
                            }
                            if (INCLUDE_CHAPTERS && Array.isArray(parsed.chapters) && parsed.chapters.length)
                                props.chapters = parsed.chapters;
                            if (Object.keys(props).length)
                                parsed.properties = props;
                        }

                        if (parsed && !INCLUDE_CHAPTERS)
                            delete parsed.chapters;

                        if (parsed && Array.isArray(parsed.entries))
                        {
                            for (let i = 0; i < parsed.entries.length; ++i)
                            {
                                const e = parsed.entries[i];
                                if (!e || !PASS_EXTRA_PROPERTIES) continue;
                                const eprops = {};
                                const ekeys = ["description", "channel", "uploader", "tags",
                                    "categories", "view_count", "like_count", "duration_string",
                                    "upload_date", "webpage_url"];
                                for (let k = 0; k < ekeys.length; ++k)
                                {
                                    const key = ekeys[k];
                                    if (e[key] !== undefined && e[key] !== null)
                                        eprops[key] = e[key];
                                }
                                if (INCLUDE_CHAPTERS && Array.isArray(e.chapters) && e.chapters.length)
                                    eprops.chapters = e.chapters;
                                if (Object.keys(eprops).length)
                                    e.properties = eprops;
                                if (!INCLUDE_CHAPTERS)
                                    delete e.chapters;
                            }
                        }

                        resolve(parsed || JSON.parse(output));
                    }
                });
            });
        },

        isSupportedSource: function(url)
        {
            return false;
        },

        supportedSourceCheckPriority: function()
        {
            return 0;
        },

        isPossiblySupportedSource: function(obj)
        {
            if (obj.contentType && !/^text\/html(;.*)?$/.test(obj.contentType))
                return false;
            if (obj.resourceSize !== -1 &&
                    (obj.resourceSize === 0 || obj.resourceSize > 3*1024*1024))
            {
                return false;
            }
            return /^https?:\/\//.test(obj.url);
        },

        overrideUrlPolicy: function(url)
        {
            return true;
        },
        
        minIntevalBetweenQueryInfoDownloads: function()
        {
            return 300;
        },
        
        checkBrowser: function(requestId, interactive, browser)
        {
            console.log("Checking browser support (", browser, ")...");
            
            return launchPythonScript(requestId, interactive, "yt-dlp/yt_dlp/__main__.py", ['--cookies-from-browser', browser, 'e692ec362191442c960a761ac6b84878://test.test'])
            .then(function(obj)
            {
                logPythonResult(obj);

                return new Promise(function (resolve, reject)
                {
                    let isSupported = /"e692ec362191442c960a761ac6b84878"/.test(obj.errorOutput);
                        
                    console.log(browser, " supported: ", isSupported);
                        
                    g_browsers[browser] = isSupported;
                        
                    resolve();
                });
            });
        }
    };

    return new MsAbstractParser();
}());
