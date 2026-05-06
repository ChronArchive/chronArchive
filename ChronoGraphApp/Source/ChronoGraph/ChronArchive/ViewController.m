#import "ViewController.h"
#import <AudioToolbox/AudioToolbox.h>
#import <AudioToolbox/AudioServices.h>
#import <AVFoundation/AVFoundation.h>
#import <math.h>

#ifndef AVAudioSessionPortOverrideNone
#define AVAudioSessionPortOverrideNone 0
#endif
#ifndef AVAudioSessionPortOverrideSpeaker
#define AVAudioSessionPortOverrideSpeaker 1
#endif

extern NSString * const CGAPNSTokenRefreshedNotification;

/* Forward declarations for the inline CGVoIP manager (full impl at file bottom). */
static void CGVoIPStart(int64_t callId, NSString *token, NSString *base, UIWebView *webView);
static void CGVoIPStop(void);
static NSDictionary *CGParseQueryString(NSString *q);
static void CGVoIPRingStart(void);
static void CGVoIPRingStop(void);
static void CGVoIPSetSpeakerEnabled(BOOL enabled);
static void CGVoIPNotify(NSString *state, NSString *msg);
static BOOL gMuted = NO;

static NSString * const CGReleaseInactiveWebViewsNotification = @"CGReleaseInactiveWebViewsNotification";

static NSString *CGBase64FromData(NSData *data) {
    if (!data || ![data length]) return @"";
    static char table[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    NSUInteger len = [data length];
    const unsigned char *bytes = (const unsigned char *)[data bytes];
    NSMutableString *out = [NSMutableString stringWithCapacity:((len + 2) / 3) * 4];
    NSUInteger i;
    for (i = 0; i < len; i += 3) {
        unsigned long v = 0;
        int n = 0;
        int j;
        for (j = 0; j < 3; j++) {
            v <<= 8;
            if (i + j < len) {
                v |= bytes[i + j];
                n++;
            }
        }
        for (j = 0; j < 4; j++) {
            if (j <= n) {
                int idx = (int)((v >> (18 - 6 * j)) & 0x3F);
                [out appendFormat:@"%c", table[idx]];
            } else {
                [out appendString:@"="];
            }
        }
    }
    return out;
}

@interface ViewController ()
/* On armv6 builds we compile without ARC, so object properties must retain. */
#if __has_feature(objc_arc)
@property (nonatomic, strong) UIWebView *webView;
@property (nonatomic, strong) NSURL *initialURL;  /* page this VC loads */
@property (nonatomic, strong) NSURL *rootURL;     /* tab root — nav bar hidden when here */
@property (nonatomic, copy) NSString *imagePickerTarget;
@property (nonatomic, assign) BOOL webViewInitPending;
#else
@property (nonatomic, retain) UIWebView *webView;
@property (nonatomic, retain) NSURL *initialURL;  /* page this VC loads */
@property (nonatomic, retain) NSURL *rootURL;     /* tab root — nav bar hidden when here */
@property (nonatomic, copy) NSString *imagePickerTarget;
@property (nonatomic, assign) BOOL webViewInitPending;
#endif
- (void)openChatProfileWithUID:(NSString *)uid;
@end

@implementation ViewController
@synthesize webView = _webView;
@synthesize initialURL = _initialURL;
@synthesize rootURL = _rootURL;
@synthesize imagePickerTarget = _imagePickerTarget;
@synthesize webViewInitPending = _webViewInitPending;

- (BOOL)shouldUseSingleWebViewMode {
    NSInteger major = (NSInteger)[[[UIDevice currentDevice] systemVersion] intValue];
    /* iOS 26+ is more stable with persistent per-tab webviews.
       Single-webview recycling can cause full tab reloads and blank states there. */
    return (major >= 13 && major < 26);
}

- (void)teardownWebViewPreservingURL:(BOOL)preserveURL {
    if (!self.webView) return;
    if (preserveURL) {
        NSURL *cur = self.webView.request.URL;
        if (cur) self.initialURL = cur;
    }
    self.webView.delegate = nil;
    [self.webView stopLoading];
    [self.webView removeFromSuperview];
    self.webView = nil;
}

- (void)onReleaseInactiveWebViews:(NSNotification *)note {
    if (note.object == self) return;
    if (![self shouldUseSingleWebViewMode]) return;
    if (!(self.isViewLoaded && self.view.window != nil)) {
        [self teardownWebViewPreservingURL:YES];
    }
}

- (BOOL)canCreateWebViewNow {
    UIApplication *app = [UIApplication sharedApplication];
    if ([app respondsToSelector:@selector(applicationState)]) {
        return app.applicationState == UIApplicationStateActive;
    }
    return YES;
}

- (void)ensureWebViewCreated {
    NSInteger legacyMajor;

    if (self.webView) {
        /* Recovery: if a webview exists but has no main request, reload tab root. */
        if (!self.webView.request.URL && self.initialURL) {
            NSLog(@"[CGNATIVE] recover loadRequest %@", self.initialURL.absoluteString);
            [self.webView loadRequest:[NSURLRequest requestWithURL:self.initialURL]];
        }
        return;
    }

    if ([self shouldUseSingleWebViewMode]) {
        [[NSNotificationCenter defaultCenter] postNotificationName:CGReleaseInactiveWebViewsNotification object:self];
    }

    if (![self canCreateWebViewNow]) {
        /* Allow repeated retries — on iOS 26 the app state may not be .active yet
           when viewDidAppear fires; keep polling until the state settles. */
        [NSObject cancelPreviousPerformRequestsWithTarget:self selector:@selector(ensureWebViewCreated) object:nil];
        [self performSelector:@selector(ensureWebViewCreated) withObject:nil afterDelay:0.25];
        return;
    }

    self.webViewInitPending = NO;
    legacyMajor = (NSInteger)[[[UIDevice currentDevice] systemVersion] intValue];

    self.webView = [[UIWebView alloc] initWithFrame:self.view.bounds];
    self.webView.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
    self.webView.delegate = self;
    /* On iOS 2G/3G-era WebKit, scalesPageToFit can break post-load scroll geometry on dynamic pages. */
    self.webView.scalesPageToFit = (legacyMajor > 0 && legacyMajor <= 4) ? NO : YES;
    /* mediaPlaybackRequiresUserAction and allowsInlineMediaPlayback are iOS 4+ */
    if ([self.webView respondsToSelector:@selector(setMediaPlaybackRequiresUserAction:)])
        self.webView.mediaPlaybackRequiresUserAction = NO;
    if ([self.webView respondsToSelector:@selector(setAllowsInlineMediaPlayback:)])
        self.webView.allowsInlineMediaPlayback = YES;
    /* scrollView is iOS 5+ — guard so the same binary can be rebuilt for armv6/iOS 3 later */
    if ([self.webView respondsToSelector:@selector(scrollView)]) {
        self.webView.scrollView.bounces = NO;
        self.webView.scrollView.scrollEnabled = YES;
    }
    [self.view addSubview:self.webView];

    if (self.initialURL) {
        NSLog(@"[CGNATIVE] loadRequest %@", self.initialURL.absoluteString);
        [self.webView loadRequest:[NSURLRequest requestWithURL:self.initialURL]];
    }
}

- (void)onAppDidBecomeActive:(NSNotification *)note {
    (void)note;
    if (self.isViewLoaded && self.view.window != nil) {
        [self ensureWebViewCreated];
    }
}

- (instancetype)initWithURL:(NSURL *)url rootURL:(NSURL *)rootURL {
    self = [super init];
    if (self) {
        self.initialURL = url;
        self.rootURL    = rootURL;
    }
    return self;
}

- (void)viewDidLoad {
    [super viewDidLoad];
    self.view.backgroundColor = [UIColor blackColor];
    NSLog(@"[CGNATIVE] viewDidLoad url=%@", self.initialURL.absoluteString);

    /* iOS 7+: stop UIKit from pushing content down under the hidden nav bar.
       Cast to id and use raw integer values to avoid iOS 5 SDK type errors. */
    if ([self respondsToSelector:@selector(setEdgesForExtendedLayout:)])
        [(id)self setValue:[NSNumber numberWithInt:0] forKey:@"edgesForExtendedLayout"];
    if ([self respondsToSelector:@selector(setAutomaticallyAdjustsScrollViewInsets:)])
        [(id)self setValue:[NSNumber numberWithBool:NO] forKey:@"automaticallyAdjustsScrollViewInsets"];
    [[NSNotificationCenter defaultCenter] addObserver:self
                                             selector:@selector(onAppDidBecomeActive:)
                                                 name:UIApplicationDidBecomeActiveNotification
                                               object:nil];
        [[NSNotificationCenter defaultCenter] addObserver:self
                                                                                         selector:@selector(onReleaseInactiveWebViews:)
                                                                                                 name:CGReleaseInactiveWebViewsNotification
                                                                                             object:nil];
        [[NSNotificationCenter defaultCenter] addObserver:self
                                                                                         selector:@selector(onAPNSTokenRefreshed:)
                                                                                                 name:CGAPNSTokenRefreshedNotification
                                                                                             object:nil];
}

/* Hide the native nav bar on the tab root page; show it on any sub-page so the
   back button is visible.  Called on appear so it also fires correctly after a pop. */
- (void)viewWillAppear:(BOOL)animated {
    [super viewWillAppear:animated];
    BOOL isRoot = [self.initialURL.absoluteString isEqualToString:self.rootURL.absoluteString];
    NSLog(@"[CGNATIVE] viewWillAppear url=%@ isRoot=%d", self.initialURL.absoluteString, isRoot ? 1 : 0);
    [self.navigationController setNavigationBarHidden:isRoot animated:animated];
}

- (void)viewDidAppear:(BOOL)animated {
    [super viewDidAppear:animated];
    [NSObject cancelPreviousPerformRequestsWithTarget:self selector:@selector(ensureWebViewCreated) object:nil];
    /* Call immediately — the user just tapped this tab so the app is guaranteed active.
       A zero-delay deferred call previously caused a race on iOS 26 where applicationState
       had not yet transitioned to UIApplicationStateActive. */
    [self ensureWebViewCreated];
}

- (void)viewDidDisappear:(BOOL)animated {
    [super viewDidDisappear:animated];
    if ([self shouldUseSingleWebViewMode]) {
        [self teardownWebViewPreservingURL:YES];
    }
}

- (void)injectPushBridge {
    if (!self.webView) return;
    NSString *apnsToken = [[NSUserDefaults standardUserDefaults] objectForKey:@"cg_apns_token"];
    NSString *apnsEnv   = [[NSUserDefaults standardUserDefaults] objectForKey:@"cg_apns_environment"];
    if (!apnsEnv || ![apnsEnv length]) apnsEnv = @"sandbox";
    if (![apnsEnv isEqualToString:@"sandbox"] && ![apnsEnv isEqualToString:@"production"]) {
        apnsEnv = @"sandbox";
    }
    if (![[NSBundle mainBundle] pathForResource:@"embedded" ofType:@"mobileprovision"]) {
        apnsEnv = @"sandbox";
    }
    if (!apnsToken || ![apnsToken length]) return;
    NSString *escTok = [apnsToken stringByReplacingOccurrencesOfString:@"\\" withString:@"\\\\"];
    escTok = [escTok stringByReplacingOccurrencesOfString:@"'" withString:@"\\'"];
    NSString *escEnv = [apnsEnv stringByReplacingOccurrencesOfString:@"\\" withString:@"\\\\"];
    escEnv = [escEnv stringByReplacingOccurrencesOfString:@"'" withString:@"\\'"];
    NSString *js = [NSString stringWithFormat:
        @"(function(){"
            "window.__cgApnsToken='%@';"
            "window.__cgApnsEnvironment='%@';"
            "try{"
                "var t=window.__cgApnsToken||'';"
                "var env=(window.__cgApnsEnvironment==='production')?'production':'sandbox';"
                "function cgSyncPush(){"
                    "var s='';"
                    "try{s=localStorage.getItem('cg_t')||localStorage.getItem('hm_t')||'';}catch(e1){}"
                    "if(!(t&&s)) return;"
                    "var k='apns:'+t+':'+s+':'+env;"
                    "if(window.__cgApnsRegKey!==k){window.__cgApnsRegKey=k;}"
                    "var xhr=new XMLHttpRequest();"
                    "xhr.open('POST','https://chat.chronarchive.com/api/apns/register',true);"
                    "xhr.setRequestHeader('Content-Type','application/json');"
                    "xhr.setRequestHeader('X-CG-Token',s);"
                    "try{xhr.timeout=12000;}catch(e2){}"
                    "xhr.send(JSON.stringify({device_token:t,environment:env,platform:'ios'}));"
                    "var br=new XMLHttpRequest();"
                    "br.open('GET','https://chat.chronarchive.com/api/badge',true);"
                    "br.setRequestHeader('X-CG-Token',s);"
                    "br.onreadystatechange=function(){"
                        "if(br.readyState===4&&br.status===200){"
                            "try{"
                                "var d=JSON.parse(br.responseText);"
                                "if(d&&d.ok&&typeof d.unread_dm==='number'){"
                                    "window.location='cgbadge://set?count='+encodeURIComponent(String(d.unread_dm));"
                                "}"
                            "}catch(pe){}"
                        "}"
                    "};"
                    "br.send();"
                "}"
                "cgSyncPush();"
                "if(!window.__cgApnsSyncTimer){window.__cgApnsSyncTimer=setInterval(cgSyncPush,15000);}"
            "}catch(e2){}"
        "})();", escTok, escEnv];
    [self.webView stringByEvaluatingJavaScriptFromString:js];
}

- (void)injectPerformanceBridge {
    if (!self.webView) return;
    NSString *js =
        @"(function(){"
            "try{"
                "if(window.__cgPerfBooted) return 'skip';"
                "window.__cgPerfBooted=1;"
                "var ua=navigator.userAgent||'';"
                "var legacy=/OS [1-4]_/.test(ua);"
                "var enabled=legacy;"
                "try{enabled=enabled||((localStorage.getItem('cg_perf')||'')==='1');}catch(e1){}"
                "window.__cgPerfEnabled=enabled?1:0;"
                "function nowMs(){"
                    "try{return (window.performance&&performance.now)?performance.now():(new Date()).getTime();}catch(e2){return (new Date()).getTime();}"
                "}"
                "window.__cgPerfNow=nowMs;"
                "window.__cgNativePerfLog=function(msg){"
                    "if(!window.__cgPerfEnabled) return;"
                    "try{"
                        "var d=document;"
                        "var n=d.createElement('iframe');"
                        "n.style.display='none';"
                        "n.src='cglog://log?m='+encodeURIComponent(String(msg||''));"
                        "(d.documentElement||d.body).appendChild(n);"
                        "setTimeout(function(){try{if(n.parentNode)n.parentNode.removeChild(n);}catch(_e){}},0);"
                    "}catch(e3){try{if(window.console&&console.log)console.log('[CG][PERF] '+msg);}catch(e4){}}"
                "};"
                "window.__cgPerfStart=function(){return window.__cgPerfEnabled?nowMs():0;};"
                "window.__cgPerfEnd=function(name,t0,extra){"
                    "if(!window.__cgPerfEnabled||!t0) return;"
                    "var dt=nowMs()-t0;"
                    "var slow=legacy?35:120;"
                    "if(dt>=slow){"
                        "window.__cgNativePerfLog('[CG][PERF] '+name+' '+Math.round(dt)+'ms'+(extra?(' '+extra):''));"
                    "}"
                "};"
                "window.__cgPerfMark=function(name,extra){"
                    "if(!window.__cgPerfEnabled) return;"
                    "window.__cgNativePerfLog('[CG][PERF] '+name+(extra?(' '+extra):''));"
                "};"
                "if(window.__cgPerfEnabled&&!window.__cgPerfXhrWrapped&&window.XMLHttpRequest){"
                    "window.__cgPerfXhrWrapped=1;"
                    "var oOpen=XMLHttpRequest.prototype.open;"
                    "var oSend=XMLHttpRequest.prototype.send;"
                    "XMLHttpRequest.prototype.open=function(method,url){"
                        "this.__cgPerfMethod=method||'GET';"
                        "this.__cgPerfUrl=url||'';"
                        "return oOpen.apply(this,arguments);"
                    "};"
                    "XMLHttpRequest.prototype.send=function(){"
                        "var xhr=this;"
                        "xhr.__cgPerfT0=nowMs();"
                        "function done(){"
                            "if(!xhr||!xhr.__cgPerfT0) return;"
                            "var dt=nowMs()-xhr.__cgPerfT0;"
                            "var slowNet=legacy?450:900;"
                            "if(dt>=slowNet){"
                                "window.__cgNativePerfLog('[CG][PERF][NET] '+(xhr.__cgPerfMethod||'GET')+' '+(xhr.__cgPerfUrl||'')+' '+Math.round(dt)+'ms status='+(xhr.status||0));"
                            "}"
                            "xhr.__cgPerfT0=0;"
                        "}"
                        "if(xhr.addEventListener){"
                            "xhr.addEventListener('loadend',done,false);"
                        "}else{"
                            "var old=xhr.onreadystatechange;"
                            "xhr.onreadystatechange=function(){"
                                "if(xhr.readyState===4) done();"
                                "if(old) return old.apply(xhr,arguments);"
                            "};"
                        "}"
                        "return oSend.apply(this,arguments);"
                    "};"
                "}"
                "return 'ok';"
            "}catch(e){return 'err='+e;}"
        "})();";
    NSString *res = [self.webView stringByEvaluatingJavaScriptFromString:js];
    NSLog(@"[CGNATIVE] perfBridge %@", res);
}

- (void)onAPNSTokenRefreshed:(NSNotification *)note {
    (void)note;
    if (self.isViewLoaded && self.view.window != nil) {
        [self injectPushBridge];
    }
}

- (UIColor *)accentColorForName:(NSString *)name {
    if (!name) return [UIColor colorWithRed:0.35 green:0.78 blue:0.98 alpha:1.0];
    if ([name isEqualToString:@"green"]) {
        return [UIColor colorWithRed:0.39 green:0.85 blue:0.20 alpha:1.0];
    }
    return [UIColor colorWithRed:0.35 green:0.78 blue:0.98 alpha:1.0];
}

- (void)webViewDidFinishLoad:(UIWebView *)webView {
    NSLog(@"[CGNATIVE] didFinishLoad req=%@", webView.request.URL.absoluteString);

    [webView stringByEvaluatingJavaScriptFromString:@"window.__cgNativeImagePicker=1;window.__cgNativeVoIP=1;window.__cgNativeVoIPRing=1;"];
    [self injectPerformanceBridge];
    [self injectPushBridge];


    NSInteger legacyMajor = (NSInteger)[[[UIDevice currentDevice] systemVersion] intValue];

        NSString *probe = [webView stringByEvaluatingJavaScriptFromString:
                @"(function(){"
                    "try{"
                        "var b=document.body;"
                        "var id=(window.location&&window.location.href)?window.location.href:'';"
                        "var kids=b&&b.children?b.children.length:0;"
                        "var txt=b&&b.innerText?b.innerText.length:0;"
                        "return 'url='+id+' kids='+kids+' txt='+txt;"
                    "}catch(e){return 'probe_err='+e;}"
                "})();"];
        NSLog(@"[CGNATIVE] didFinishProbe %@", probe);

    /* Legacy JS blocks are only needed on iOS <= 4 (iPhone 2G/3G/3GS).
       Skipping them on modern iOS prevents synchronous WebKit calls
       from hanging the main thread after a force-quit cold restart. */
    if (legacyMajor <= 4) {
        NSString *legacyForce = [webView stringByEvaluatingJavaScriptFromString:
                @"(function(){"
                    "try{"
                        "if(!/OS [1-4]_/.test(navigator.userAgent||'')) return 'skip:not-legacy';"
                        "var href=(window.location&&window.location.href)?window.location.href:'';"
                        "if(document.body){document.body.style.opacity='1';document.body.style.visibility='visible';document.body.style.background='#f2f2f7';document.body.style.color='#1a1a1a';}"
                        "function vis(sel){var n=document.querySelectorAll?document.querySelectorAll(sel):[];for(var i=0;i<n.length;i++){n[i].style.opacity='1';n[i].style.visibility='visible';}}"
                        "if(href.indexOf('/pages/home.html')>=0) vis('#posts-feed,.post-card,.post-hd,.post-body,.post-actions');"
                        "if(href.indexOf('/pages/search.html')>=0) vis('#results-body,.res-post,.res-person,.res-web,.sug-row');"
                        "if(href.indexOf('/pages/account.html')>=0) vis('#view-signedin,#view-signedout');"
                        "if(href.indexOf('/pages/chat.html')>=0){"
                            "var a=document.querySelector?document.querySelector('.screen.active'):null;"
                            "if(a){a.style.display='block';a.style.opacity='1';a.style.visibility='visible';}"
                        "}"
                        "return 'applied-inline';"
                    "}catch(e){return 'err='+e;}"
                "})();"];
        NSLog(@"[CGNATIVE] didFinishLegacyForce %@", legacyForce);

        NSString *legacyScrollNormalize = [webView stringByEvaluatingJavaScriptFromString:
                @"(function(){"
                    "try{"
                        "if(!/OS [1-4]_/.test(navigator.userAgent||'')) return 'skip:not-legacy';"
                        "var href=(window.location&&window.location.href)?window.location.href:'';"
                        "if(href.indexOf('/pages/tools.html')>=0) return 'skip:tools';"
                        "window.__cgNativeLegacyScroll=1;"
                        "var major=/OS (\\d+)_/.exec(navigator.userAgent||'');major=major?parseInt(major[1],10):99;"
                        "if(major>4) return 'skip:not-legacy-major';"
                        "function setBase(){"
                            "var de=document.documentElement,db=document.body;"
                            "if(de){de.style.height='auto';de.style.minHeight='100%';de.style.overflow='auto';de.style.webkitOverflowScrolling='touch';}"
                            "if(db){db.style.height='auto';db.style.minHeight='100%';db.style.overflow='auto';db.style.webkitOverflowScrolling='touch';db.style.background='#f2f2f7';db.style.color='#1a1a1a';}"
                        "}"
                        "function pinHeader(el,z){if(!el)return;el.style.position='fixed';el.style.top='0';el.style.left='0';el.style.right='0';el.style.zIndex=String(z||100);}"
                        "function docFlow(el,padTop,padBottom){"
                            "if(!el)return;"
                            "el.style.position='relative';"
                            "el.style.top='0';"
                            "el.style.left='auto';"
                            "el.style.right='auto';"
                            "el.style.bottom='auto';"
                            "el.style.height='auto';"
                            "el.style.minHeight='0';"
                            "el.style.overflow='visible';"
                            "el.style.overflowY='visible';"
                            "el.style.overflowX='visible';"
                            "el.style.webkitOverflowScrolling='auto';"
                            "el.style.paddingTop=(padTop||0)+'px';"
                            "el.style.paddingBottom=(padBottom||0)+'px';"
                        "}"
                        "function run(){"
                            "try{"
                                "setBase();"
                                "if(href.indexOf('/pages/home.html')>=0){"
                                    "var hdr=document.getElementById('hdr'); if(hdr){pinHeader(hdr,100);hdr.style.height='44px';}"
                                    "var shell=document.getElementById('shell'); if(shell){shell.style.display='block';shell.style.height='auto';shell.style.overflow='visible';shell.style.position='relative';}"
                                    "docFlow(document.getElementById('feed-wrap'),44,0);"
                                "}"
                                "if(href.indexOf('/pages/search.html')>=0){"
                                    "var sb=document.getElementById('searchbar'); var tabs=document.getElementById('cat-tabs');"
                                    "var sbh=sb&&sb.offsetHeight?sb.offsetHeight:44;"
                                    "var th=tabs&&tabs.offsetHeight?tabs.offsetHeight:34;"
                                    "var shell2=document.getElementById('shell'); if(shell2){shell2.style.display='block';shell2.style.height='auto';shell2.style.overflow='visible';shell2.style.position='relative';}"
                                    "if(sb){pinHeader(sb,100);}"
                                    "if(tabs){tabs.style.position='fixed';tabs.style.top=sbh+'px';tabs.style.left='0';tabs.style.right='0';tabs.style.zIndex='101';}"
                                    "docFlow(document.getElementById('results-wrap'),sbh+th,0);"
                                "}"
                                "if(href.indexOf('/pages/account.html')>=0){"
                                    "var ah=document.getElementById('hdr'); if(ah){pinHeader(ah,100);ah.style.height='44px';}"
                                    "docFlow(document.getElementById('scroll'),44,0);"
                                    "var subs=document.getElementsByClassName?document.getElementsByClassName('subscreen'):[];"
                                    "for(var i=0;i<subs.length;i++){subs[i].style.position='relative';subs[i].style.top='0';subs[i].style.left='auto';subs[i].style.right='auto';subs[i].style.bottom='auto';subs[i].style.minHeight='100%';}"
                                    "var sh=document.getElementsByClassName?document.getElementsByClassName('sub-hdr'):[];"
                                    "for(var j=0;j<sh.length;j++){pinHeader(sh[j],201);}"
                                    "var sbd=document.getElementsByClassName?document.getElementsByClassName('sub-body'):[];"
                                    "for(var k=0;k<sbd.length;k++) docFlow(sbd[k],44,0);"
                                "}"
                                "if(href.indexOf('/pages/chat.html')>=0){"
                                    "var screens=document.getElementsByClassName?document.getElementsByClassName('screen'):[];"
                                    "for(var a=0;a<screens.length;a++){screens[a].style.position='relative';screens[a].style.top='0';screens[a].style.left='auto';screens[a].style.right='auto';screens[a].style.bottom='auto';screens[a].style.minHeight='100%';screens[a].style.overflow='visible';}"
                                    "var heads=document.getElementsByClassName?document.getElementsByClassName('header'):[];"
                                    "for(var b=0;b<heads.length;b++){pinHeader(heads[b],100);}"
                                    "var ib=document.getElementsByClassName?document.getElementsByClassName('ibar'):[];"
                                    "for(var c=0;c<ib.length;c++){ib[c].style.position='fixed';ib[c].style.left='0';ib[c].style.right='0';ib[c].style.bottom='6px';ib[c].style.height='56px';ib[c].style.paddingBottom='6px';ib[c].style.zIndex='101';}"
                                    "var sbt=document.getElementById('send-btn'); if(sbt){sbt.style.background='#8abdec';sbt.style.backgroundColor='#8abdec';sbt.style.backgroundImage='none';sbt.style.border='1px solid #5a92c9';sbt.style.borderStyle='solid';sbt.style.borderColor='#5a92c9';sbt.style.color='#fff';sbt.style.opacity='1';}"
                                    "var searchBar=document.querySelector?document.querySelector('#users-screen .search-bar'):null;"
                                    "if(searchBar){searchBar.style.position='fixed';searchBar.style.top='44px';searchBar.style.left='0';searchBar.style.right='0';searchBar.style.zIndex='101';}"
                                    "var conv=document.getElementById('convos-body'); if(conv) docFlow(conv,44,0);"
                                    "var users=document.getElementById('users-body'); if(users) docFlow(users,92,0);"
                                    "var thread=document.getElementById('thread-body'); if(thread) docFlow(thread,44,62);"
                                    "var friends=document.getElementById('friends-body'); if(friends) docFlow(friends,44,0);"
                                    "var admin=document.getElementById('admin-body'); if(admin) docFlow(admin,44,0);"
                                "}"
                                "var de=document.documentElement,db=document.body;"
                                "var sh2=Math.max(de?de.scrollHeight:0,db?db.scrollHeight:0);"
                                "if(window.console&&console.log)console.log('[CG][LEGACY_SCROLL] href='+href+' sh='+sh2+' wh='+(window.innerHeight||0)+' y='+(window.pageYOffset||0));"
                            "}catch(_e){}"
                        "}"
                        "run();"
                        "setTimeout(run,0);"
                        "setTimeout(run,120);"
                        "setTimeout(run,400);"
                        "setTimeout(run,1000);"
                        "setTimeout(run,1800);"
                        "setTimeout(run,2600);"
                        "if(!window.__cgLegacyScrollBound){"
                            "window.__cgLegacyScrollBound=1;"
                            "if(window.addEventListener){window.addEventListener('orientationchange',run,false);window.addEventListener('resize',run,false);}"
                            "window.__cgLegacyScrollTimer=setInterval(run,2000);"
                        "}"
                        "window.__cgLegacyScrollRun=run;"
                        "return 'applied-inline-docflow';"
                    "}catch(e){return 'err='+e;}"
                "})();"];
        NSLog(@"[CGNATIVE] didFinishLegacyScrollNormalize %@", legacyScrollNormalize);
    } /* end iOS <= 4 only */

    /* Defer rescue by one runloop tick + delay without GCD.
       iPhone 2G-era libSystem lacks modern dispatch symbols. */
    [NSObject cancelPreviousPerformRequestsWithTarget:self selector:@selector(runDeferredRescue) object:nil];
    [self performSelector:@selector(runDeferredRescue) withObject:nil afterDelay:0.15];
}

- (void)runDeferredRescue {
    UIWebView *capturedWebView = self.webView;
    if (!capturedWebView) return;

    NSString *rescue = [capturedWebView stringByEvaluatingJavaScriptFromString:
                @"(function(){"
                    "try{"
                        "if(window.__cgNativeRescueDone) return 'skip=already';"
                        "window.__cgNativeRescueDone=1;"
                        "var href=(window.location&&window.location.href)?window.location.href:'';"
                        "var out=[];"
                        "function add(s){out.push(s);}"
                        "if(href.indexOf('/pages/home.html')>=0){"
                            "var feed=document.getElementById('posts-feed');"
                            "var h=feed&&feed.innerHTML?feed.innerHTML:'';"
                            "var stuck=(!h)||h.length<8||h.indexOf('Loading')>=0||h.indexOf('feed-load')>=0;"
                            "if(window.S&&(/OS [1-4]_/.test(navigator.userAgent||''))&&S.feedMode!=='all'&&!S.feedModeUserSet){"
                                "try{S.feedMode='all';add('home:forceAll');}catch(efm){add('home:forceAllErr='+efm);}"
                            "}"
                            "if(feed && stuck && typeof loadFeed==='function'){"
                                "try{loadFeed(true);add('home:loadFeed');}catch(eh){add('home:err='+eh);}"
                            "}"
                        "}"
                        "if(href.indexOf('/pages/search.html')>=0){"
                            "var rb=document.getElementById('results-body');"
                            "if(rb && ((!rb.innerHTML)||rb.innerHTML.length<8) && typeof showSuggestions==='function'){"
                                "try{showSuggestions();add('search:showSuggestions');}catch(es){add('search:err='+es);}"
                            "}"
                        "}"
                        "if(href.indexOf('/pages/account.html')>=0){"
                            "var so=document.getElementById('view-signedout');"
                            "var si=document.getElementById('view-signedin');"
                            "var hidden=so&&si&&so.style.display==='none'&&si.style.display==='none';"
                            "if(hidden){"
                                "try{"
                                    "if(window.S&&S.token&&typeof afterLogin==='function'){afterLogin();add('account:afterLogin');}"
                                    "else if(typeof renderSignedOut==='function'){renderSignedOut();add('account:renderSignedOut');}"
                                "}catch(ea){add('account:err='+ea);}"
                            "}"
                        "}"
                        "if(href.indexOf('/pages/chat.html')>=0){"
                            "var active=document.querySelector?document.querySelector('.screen.active'):null;"
                            "if(!active&&!window.__cgBootStarted&&!window.__cgBootDone){"
                                "try{"
                                    "if(window.S&&!S.token){"
                                        "try{S.token=localStorage.getItem('cg_t')||localStorage.getItem('hm_t')||S.token;}catch(et1){}"
                                        "try{if(!S.userId)S.userId=parseInt(localStorage.getItem('cg_u')||localStorage.getItem('hm_u')||'0',10)||null;}catch(et2){}"
                                        "try{if(!S.username)S.username=localStorage.getItem('cg_n')||localStorage.getItem('hm_n')||S.username;}catch(et3){}"
                                    "}"
                                    "if(window.S&&S.token&&typeof goConvos==='function'){goConvos();add('chat:goConvos');}"
                                    "else if(typeof show==='function'){show('auth-screen');add('chat:showAuth');}"
                                "}catch(ec){add('chat:err='+ec);}"
                            "}"
                        "}"
                        "if(out.length){try{console.log('[CG][RESCUE] '+out.join('|'));}catch(e){}}"
                        "return out.join('|')||'none';"
                    "}catch(e){return 'fatal='+e;}"
                "})();"];
    NSLog(@"[CGNATIVE] didFinishRescue %@", rescue);
}

- (void)didReceiveMemoryWarning {
    [super didReceiveMemoryWarning];
    /* Free the WebView when this tab is not on screen to reclaim memory.
       viewDidLoad re-creates it and reloads from initialURL next time the tab is shown. */
    if (self.isViewLoaded && self.view.window == nil) {
        [self teardownWebViewPreservingURL:YES];
        self.view = nil; /* causes viewDidLoad to be called again when tab is re-selected */
    }
}

- (void)dealloc {
    [[NSNotificationCenter defaultCenter] removeObserver:self];
    [NSObject cancelPreviousPerformRequestsWithTarget:self selector:@selector(ensureWebViewCreated) object:nil];
    [NSObject cancelPreviousPerformRequestsWithTarget:self selector:@selector(runDeferredRescue) object:nil];
    self.webView.delegate = nil;
#if !__has_feature(objc_arc)
    [_webView release];
    [_initialURL release];
    [_rootURL release];
    [_imagePickerTarget release];
    [super dealloc];
#endif
}

#pragma mark - UIWebViewDelegate

- (BOOL)webView:(UIWebView *)webView shouldStartLoadWithRequest:(NSURLRequest *)request
                                                 navigationType:(UIWebViewNavigationType)navigationType {
    NSURL      *url    = request.URL;
    NSString   *scheme = url.scheme.lowercaseString;

    if ([scheme isEqualToString:@"cgbadge"]) {
        /* cgbadge://set?count=N — update the app badge number */
        NSString *query = [url query];
        NSInteger badgeCount = 0;
        if (query) {
            for (NSString *pair in [query componentsSeparatedByString:@"&"]) {
                NSArray *parts = [pair componentsSeparatedByString:@"="];
                if ([parts count] == 2 && [[parts objectAtIndex:0] isEqualToString:@"count"]) {
                    NSString *val = [[parts objectAtIndex:1]
                        stringByReplacingPercentEscapesUsingEncoding:NSUTF8StringEncoding];
                    badgeCount = [val integerValue];
                    break;
                }
            }
        }
        [[UIApplication sharedApplication] setApplicationIconBadgeNumber:badgeCount];
        return NO;
    }

    if ([scheme isEqualToString:@"cglog"]) {
        NSString *query = [url query];
        NSString *msg = @"";
        if (query) {
            NSArray *pairs = [query componentsSeparatedByString:@"&"];
            for (NSString *pair in pairs) {
                NSRange eq = [pair rangeOfString:@"="];
                if (eq.location != NSNotFound) {
                    NSString *key = [pair substringToIndex:eq.location];
                    if (![key isEqualToString:@"m"]) continue;
                    NSString *encoded = [pair substringFromIndex:(eq.location + 1)];
                    msg = [encoded stringByReplacingPercentEscapesUsingEncoding:NSUTF8StringEncoding] ?: @"";
                    break;
                }
            }
        }
        NSLog(@"[CGJS] %@", msg);
        return NO;
    }

    if ([scheme isEqualToString:@"cgswitch"]) {
        NSString *host = [[url host] lowercaseString];
        NSString *uid = nil;
        NSString *query = [url query];
        if (query) {
            NSArray *pairs = [query componentsSeparatedByString:@"&"];
            for (NSString *pair in pairs) {
                NSArray *parts = [pair componentsSeparatedByString:@"="];
                if ([parts count] == 2 && [[parts objectAtIndex:0] isEqualToString:@"uid"]) {
                    uid = [parts objectAtIndex:1];
                    break;
                }
            }
        }
        if ([host isEqualToString:@"chat"]) {
            UITabBarController *tabs = self.navigationController.tabBarController;
            if (tabs && [tabs.viewControllers count] > 2) {
                tabs.selectedIndex = 2;
                UIViewController *tabNav = [tabs.viewControllers objectAtIndex:2];
                if ([tabNav isKindOfClass:[UINavigationController class]]) {
                    UINavigationController *nav = (UINavigationController *)tabNav;
                    if ([nav.viewControllers count] > 0) {
                        UIViewController *root = [nav.viewControllers objectAtIndex:0];
                        if ([root isKindOfClass:[ViewController class]]) {
                            ViewController *vc = (ViewController *)root;
                            if (uid && [uid length] > 0) {
                                [vc openChatProfileWithUID:uid];
                            }
                        }
                    }
                }
            }
            return NO;
        }
        return NO;
    }

    if ([scheme isEqualToString:@"cgpick"]) {
        NSString *host = [[url host] lowercaseString];
        if ([host isEqualToString:@"avatar"]) {
            [self performSelector:@selector(presentAvatarImagePicker) withObject:nil afterDelay:0.0];
            return NO;
        }
        if ([host isEqualToString:@"post"]) {
            [self performSelector:@selector(presentPostImagePicker) withObject:nil afterDelay:0.0];
            return NO;
        }
        if ([host isEqualToString:@"dm"]) {
            [self performSelector:@selector(presentDmImagePicker) withObject:nil afterDelay:0.0];
            return NO;
        }
    }

    if ([scheme isEqualToString:@"cgshare"]) {
        /* cgshare://send?t=<text>&u=<url> — present UIActivityViewController */
        NSString *q = [url query];
        NSString *txt = @"";
        NSString *shareUrl = @"";
        if (q) {
            for (NSString *pair in [q componentsSeparatedByString:@"&"]) {
                NSRange eq = [pair rangeOfString:@"="];
                if (eq.location == NSNotFound) continue;
                NSString *k = [pair substringToIndex:eq.location];
                NSString *v = [[pair substringFromIndex:(eq.location + 1)]
                    stringByReplacingPercentEscapesUsingEncoding:NSUTF8StringEncoding] ?: @"";
                if ([k isEqualToString:@"t"]) txt = v;
                else if ([k isEqualToString:@"u"]) shareUrl = v;
            }
        }
        NSMutableArray *items = [NSMutableArray array];
        if ([txt length] > 0) [items addObject:txt];
        if ([shareUrl length] > 0) {
            NSURL *u = [NSURL URLWithString:shareUrl];
            if (u) [items addObject:u]; else [items addObject:shareUrl];
        }
        if ([items count] == 0) return NO;
        Class avc = NSClassFromString(@"UIActivityViewController");
        if (avc && [self respondsToSelector:@selector(presentViewController:animated:completion:)]) {
            id sheet = [[avc alloc] initWithActivityItems:items applicationActivities:nil];
            /* Anchor popover for iPad */
            if ([sheet respondsToSelector:@selector(popoverPresentationController)]) {
                id pop = [sheet performSelector:@selector(popoverPresentationController)];
                if (pop) {
                    [pop setValue:self.view forKey:@"sourceView"];
                    NSValue *rectVal = [NSValue valueWithCGRect:CGRectMake(self.view.bounds.size.width/2, self.view.bounds.size.height-1, 1, 1)];
                    [pop setValue:rectVal forKey:@"sourceRect"];
                }
            }
            [self presentViewController:sheet animated:YES completion:nil];
#if !__has_feature(objc_arc)
            [sheet release];
#endif
        } else {
            /* iOS < 6 — fall back to mailto: with the text/url body */
            NSString *body = txt;
            if ([shareUrl length] > 0) body = [body length] > 0 ? [NSString stringWithFormat:@"%@ %@", body, shareUrl] : shareUrl;
            NSString *mailto = [NSString stringWithFormat:@"mailto:?body=%@",
                [body stringByAddingPercentEscapesUsingEncoding:NSUTF8StringEncoding] ?: @""];
            NSURL *m = [NSURL URLWithString:mailto];
            if (m) [[UIApplication sharedApplication] openURL:m];
        }
        return NO;
    }

    /* about: and javascript: — always allow (inline handlers, about:blank etc.) */
    if ([scheme isEqualToString:@"about"] ||
        [scheme isEqualToString:@"javascript"]) return YES;

    /* cgvoip://start?call_id=N&t=TOKEN&base=URL — start native AudioQueue VoIP relay
       cgvoip://stop                              — tear down */
    if ([scheme isEqualToString:@"cgvoip"]) {
        NSString *host = [[url host] lowercaseString];
        if ([host isEqualToString:@"stop"]) {
            CGVoIPRingStop();
            CGVoIPStop();
            return NO;
        }
        if ([host isEqualToString:@"mute"]) {
            NSDictionary *q = CGParseQueryString([url query]);
            NSString *state = [[q objectForKey:@"state"] lowercaseString];
            gMuted = ([state isEqualToString:@"on"] || [state isEqualToString:@"start"]);
            return NO;
        }
        if ([host isEqualToString:@"speaker"]) {
            NSDictionary *q = CGParseQueryString([url query]);
            NSString *state = [[q objectForKey:@"state"] lowercaseString];
            CGVoIPSetSpeakerEnabled([state isEqualToString:@"on"] || [state isEqualToString:@"start"]);
            return NO;
        }
        if ([host isEqualToString:@"ring"]) {
            NSDictionary *q = CGParseQueryString([url query]);
            NSString *state = [[q objectForKey:@"state"] lowercaseString];
            if ([state isEqualToString:@"start"]) CGVoIPRingStart();
            else CGVoIPRingStop();
            return NO;
        }
        if ([host isEqualToString:@"start"]) {
            NSDictionary *q = CGParseQueryString([url query]);
            NSString *cid  = [q objectForKey:@"call_id"];
            NSString *tok  = [q objectForKey:@"t"];
            NSString *base = [q objectForKey:@"base"];
            int64_t callId = (cid ? (int64_t)[cid longLongValue] : 0);
            if (callId > 0 && tok.length > 0 && base.length > 0) {
                CGVoIPRingStop();
                CGVoIPStart(callId, tok, base, webView);
            } else {
                NSLog(@"[CGVOIP] start ignored bad params");
            }
            return NO;
        }
        return NO;
    }

     /* cgopen:// — JS-side explicit URL open (search results, web search fallback).
         Open inside app webview (pushed VC) instead of bouncing to Safari. */
    if ([scheme isEqualToString:@"cgopen"]) {
        NSString *query = [url query];
        NSString *target = nil;
        if (query) {
            NSArray *pairs = [query componentsSeparatedByString:@"&"];
            for (NSString *pair in pairs) {
                NSArray *parts = [pair componentsSeparatedByString:@"="];
                if ([parts count] >= 2 && [[parts objectAtIndex:0] isEqualToString:@"u"]) {
                    /* Re-join remaining parts in case the value itself contained '=' */
                    NSMutableArray *valParts = [parts mutableCopy];
                    [valParts removeObjectAtIndex:0];
                    NSString *encoded = [valParts componentsJoinedByString:@"="];
                    target = [encoded stringByReplacingPercentEscapesUsingEncoding:NSUTF8StringEncoding];
                    break;
                }
            }
        }
        if (target && [target length] > 0) {
            NSInteger legacyMajor = (NSInteger)[[[UIDevice currentDevice] systemVersion] intValue];
            if (legacyMajor > 0 && legacyMajor <= 4 && [target hasPrefix:@"https://"]) {
                target = [@"http://" stringByAppendingString:[target substringFromIndex:8]];
            }
            NSURL *extURL = [NSURL URLWithString:target];
            if (extURL) {
                ViewController *next = [[ViewController alloc] initWithURL:extURL rootURL:self.rootURL];
                next.title = [extURL host];
                [self.navigationController pushViewController:next animated:YES];
            }
        }
        return NO;
    }

    /* Non-file / non-http(s) schemes (itms-services:, mailto:, tel:, etc.) — hand to system */
    if (![scheme isEqualToString:@"file"] &&
        ![scheme isEqualToString:@"http"]  &&
        ![scheme isEqualToString:@"https"]) {
        if ([[UIApplication sharedApplication] canOpenURL:url]) {
            [[UIApplication sharedApplication] openURL:url];
        }
        return NO;
    }

    /* User-initiated navigations (link tap or form submit) open in a pushed VC so the
       native back button works.  Sub-resource loads (images, scripts, XHR, iframes)
       fall through with return YES. */
    BOOL isUserNav = (navigationType == UIWebViewNavigationTypeLinkClicked ||
                      navigationType == UIWebViewNavigationTypeFormSubmitted);
    if (isUserNav) {
        /* Skip same-page anchor links — strip fragment and compare paths */
        if ([scheme isEqualToString:@"file"]) {
            NSString *curBase  = webView.request.URL.absoluteString;
            NSString *nextBase = url.absoluteString;
            NSRange cr = [curBase  rangeOfString:@"#"];
            NSRange nr = [nextBase rangeOfString:@"#"];
            if (cr.location != NSNotFound) curBase  = [curBase  substringToIndex:cr.location];
            if (nr.location != NSNotFound) nextBase = [nextBase substringToIndex:nr.location];
            if ([curBase isEqualToString:nextBase]) return YES;
        }

        ViewController *next = [[ViewController alloc] initWithURL:url rootURL:self.rootURL];
        if ([scheme isEqualToString:@"file"]) {
            NSString *leaf = [[url path] lastPathComponent];
            next.title = [[leaf stringByDeletingPathExtension] capitalizedString];
        } else {
            next.title = [url host];
        }
        [self.navigationController pushViewController:next animated:YES];
        return NO;
    }

    return YES;
}

- (void)openChatProfileWithUID:(NSString *)uid {
    if (!uid || [uid length] == 0) return;
    NSString *js = [NSString stringWithFormat:@"try{openUserProfile(%@,'','home-screen');}catch(e){}", uid];
    if (self.webView) {
        NSURL *cur = self.webView.request.URL;
        if (!cur || [cur.absoluteString rangeOfString:self.rootURL.absoluteString].location == NSNotFound) {
            NSURL *target = [NSURL URLWithString:[NSString stringWithFormat:@"%@#user-%@", self.rootURL.absoluteString, uid]];
            [self.webView loadRequest:[NSURLRequest requestWithURL:target]];
        } else {
            [self.webView stringByEvaluatingJavaScriptFromString:js];
        }
    } else {
        self.initialURL = [NSURL URLWithString:[NSString stringWithFormat:@"%@#user-%@", self.rootURL.absoluteString, uid]];
    }
}

- (void)presentImagePickerForTarget:(NSString *)target {
    self.imagePickerTarget = target;
    UIImagePickerControllerSourceType source = UIImagePickerControllerSourceTypePhotoLibrary;
    if (![UIImagePickerController isSourceTypeAvailable:source]) {
        source = UIImagePickerControllerSourceTypeSavedPhotosAlbum;
    }
    if (![UIImagePickerController isSourceTypeAvailable:source]) {
        NSLog(@"[CGNATIVE] avatarPicker unavailable");
        return;
    }
    UIImagePickerController *picker = [[UIImagePickerController alloc] init];
    picker.delegate = self;
    picker.sourceType = source;
    picker.allowsEditing = NO;
    if ([self respondsToSelector:@selector(presentViewController:animated:completion:)]) {
        [self presentViewController:picker animated:YES completion:nil];
    } else {
        [self presentModalViewController:picker animated:YES];
    }
#if !__has_feature(objc_arc)
    [picker release];
#endif
}

- (void)presentAvatarImagePicker {
    [self presentImagePickerForTarget:@"avatar"];
}

- (void)presentPostImagePicker {
    [self presentImagePickerForTarget:@"post"];
}

- (void)presentDmImagePicker {
    [self presentImagePickerForTarget:@"dm"];
}

- (UIImage *)scaledImageForAvatar:(UIImage *)image maxEdge:(CGFloat)maxEdge {
    if (!image) return nil;
    CGSize sz = image.size;
    if (sz.width <= 0 || sz.height <= 0) return image;
    CGFloat edge = sz.width > sz.height ? sz.width : sz.height;
    if (edge <= maxEdge) return image;
    CGFloat scale = maxEdge / edge;
    CGSize out = CGSizeMake((CGFloat)((int)(sz.width * scale)), (CGFloat)((int)(sz.height * scale)));
    UIGraphicsBeginImageContext(out);
    [image drawInRect:CGRectMake(0, 0, out.width, out.height)];
    UIImage *resized = UIGraphicsGetImageFromCurrentImageContext();
    UIGraphicsEndImageContext();
    return resized ? resized : image;
}

- (void)imagePickerControllerDidCancel:(UIImagePickerController *)picker {
    self.imagePickerTarget = nil;
    if ([picker respondsToSelector:@selector(dismissViewControllerAnimated:completion:)]) {
        [picker dismissViewControllerAnimated:YES completion:nil];
    } else {
        [picker dismissModalViewControllerAnimated:YES];
    }
}

- (void)imagePickerController:(UIImagePickerController *)picker didFinishPickingMediaWithInfo:(NSDictionary *)info {
    UIImage *img = [info objectForKey:UIImagePickerControllerOriginalImage];
    if (!img) img = [info objectForKey:UIImagePickerControllerEditedImage];
    BOOL isAvatar = [self.imagePickerTarget isEqualToString:@"avatar"];
    BOOL isDm     = [self.imagePickerTarget isEqualToString:@"dm"];

    /* Tier the post-image cost by device class so iPhone 2G / 3G / 3GS finish in
       a couple of seconds instead of stalling SpringBoard for 10 s.
       - Non-retina (scale==1) on iOS<=4: 640px / Q0.45 \u2192 ~30\u201360 KB.
       - Non-retina on iOS 5\u20136 (3GS):    800px / Q0.55 \u2192 ~60\u2013110 KB.
       - Retina @2x on iOS<=8 (4/4S/5/5c):  1200px / Q0.70.
       - Retina @2x+ on iOS 9+:             1600px / Q0.80 (was the old default).
       Avatars stay at 256/Q0.80 \u2014 already tiny.
       NOTE: server-side recompression / square-crop is planned (see repo memory)
       so these client tiers are temporary and conservative. */
    CGFloat scale = 1.0f;
    NSInteger osMajor = 2;
    if ([[UIScreen mainScreen] respondsToSelector:@selector(scale)]) scale = [[UIScreen mainScreen] scale];
    NSString *sysVer = [[UIDevice currentDevice] systemVersion];
    if (sysVer && [sysVer length] > 0) osMajor = [[[sysVer componentsSeparatedByString:@"."] objectAtIndex:0] integerValue];

    CGFloat maxEdge;
    CGFloat jpegQuality;
    if (isAvatar) {
        maxEdge = 256.0f;
        jpegQuality = 0.80f;
    } else if (scale <= 1.0f && osMajor <= 4) {
        maxEdge = 640.0f;  jpegQuality = 0.45f;
    } else if (scale <= 1.0f && osMajor <= 6) {
        maxEdge = 800.0f;  jpegQuality = 0.55f;
    } else if (osMajor <= 8) {
        maxEdge = 1200.0f; jpegQuality = 0.70f;
    } else {
        maxEdge = 1600.0f; jpegQuality = 0.80f;
    }
    NSString *callback = isAvatar ? @"__cgNativeAvatarPicked" : (isDm ? @"__cgNativeDmPicked" : @"__cgNativePostPicked");
    NSString *kindTag  = isAvatar ? @"avatar" : (isDm ? @"dm" : @"post");

    /* Dismiss the picker immediately so the UI returns to the app while we encode
       the image in the background. Previously the picker stayed up during scale +
       JPEG + base64 + JS-eval, which on iPhone 2G blew through the 10 s SpringBoard
       fence and caused "wait_fences: failed to receive reply". */
    if ([picker respondsToSelector:@selector(dismissViewControllerAnimated:completion:)]) {
        [picker dismissViewControllerAnimated:YES completion:nil];
    } else {
        [picker dismissModalViewControllerAnimated:YES];
    }
    self.imagePickerTarget = nil;

    UIImage *srcImg = img;
    UIWebView *targetWeb = self.webView;
    NSDate *t0 = [NSDate date];
    NSLog(@"[CGNATIVE] %@Picker begin osMajor=%ld scale=%.1f maxEdge=%.0f q=%.2f srcW=%.0f srcH=%.0f",
          kindTag, (long)osMajor, scale, maxEdge, jpegQuality,
          srcImg ? srcImg.size.width : 0, srcImg ? srcImg.size.height : 0);

    /* Inline encoder block, defined once and dispatched either on a GCD background
       queue (iOS >= 4) or synchronously on the main thread (iOS 3 / iPhone 2G,
       which predates libdispatch \u2014 calling dispatch_* there crashes the app). */
    void (^encodeBlock)(void) = ^{
        NSDate *tScale = [NSDate date];
        UIImage *scaled = [self scaledImageForAvatar:srcImg maxEdge:maxEdge];
        NSTimeInterval scaleMs = -[tScale timeIntervalSinceNow] * 1000.0;

        NSDate *tJpeg = [NSDate date];
        NSData *jpeg = UIImageJPEGRepresentation(scaled, jpegQuality);
        NSTimeInterval jpegMs = -[tJpeg timeIntervalSinceNow] * 1000.0;

        NSDate *tB64 = [NSDate date];
        NSString *b64 = CGBase64FromData(jpeg);
        NSTimeInterval b64Ms = -[tB64 timeIntervalSinceNow] * 1000.0;

        NSLog(@"[CGNATIVE] %@Picker scale dt=%.0fms outW=%.0f outH=%.0f", kindTag, scaleMs,
              scaled ? scaled.size.width : 0, scaled ? scaled.size.height : 0);
        NSLog(@"[CGNATIVE] %@Picker jpeg dt=%.0fms bytes=%lu q=%.2f", kindTag, jpegMs, (unsigned long)[jpeg length], jpegQuality);
        NSLog(@"[CGNATIVE] %@Picker b64  dt=%.0fms chars=%lu",       kindTag, b64Ms,  (unsigned long)[b64 length]);

        if (!b64 || [b64 length] == 0) {
            NSLog(@"[CGNATIVE] %@Picker failed encode", kindTag);
            return;
        }
        NSString *dataURL = [NSString stringWithFormat:@"data:image/jpeg;base64,%@", b64];
        NSString *escaped = [dataURL stringByReplacingOccurrencesOfString:@"\\" withString:@"\\\\"];
        escaped = [escaped stringByReplacingOccurrencesOfString:@"'" withString:@"\\'"];
        NSString *js = [NSString stringWithFormat:@"(function(){if(window.%@){window.%@('%@');}})();", callback, callback, escaped];

        void (^evalBlock)(void) = ^{
            NSDate *tEval = [NSDate date];
            [targetWeb stringByEvaluatingJavaScriptFromString:js];
            NSTimeInterval evalMs = -[tEval timeIntervalSinceNow] * 1000.0;
            NSTimeInterval totalMs = -[t0 timeIntervalSinceNow] * 1000.0;
            NSLog(@"[CGNATIVE] %@Picker eval dt=%.0fms total=%.0fms bytes=%lu",
                  kindTag, evalMs, totalMs, (unsigned long)[jpeg length]);
        };
        if (osMajor >= 4) {
            dispatch_async(dispatch_get_main_queue(), evalBlock);
        } else {
            evalBlock();
        }
    };

    if (osMajor >= 4) {
        /* GCD path: scale + JPEG + base64 off the main thread; only the JS eval
           hops back to main. Frees SpringBoard from the 10 s fence. */
        dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), encodeBlock);
    } else {
        /* iOS 3 / iPhone 2G: no libdispatch. Run synchronously on main. The
           picker has already been dismissed, and the iPhone-2G tier (640 px,
           Q 0.45, ~50 KB) finishes in well under the SpringBoard fence. */
        encodeBlock();
    }
}

- (void)webView:(UIWebView *)webView didFailLoadWithError:(NSError *)error {
    NSLog(@"[CGNATIVE] didFailLoad url=%@ code=%ld desc=%@", webView.request.URL.absoluteString, (long)error.code, error.localizedDescription);
    /* Ignore normal cancellations (redirect / new navigation started) */
    if (error.code == -999 || error.code == 102) return;
    /* Ignore sub-resource failures (images/media/custom schemes) so one bad asset does not replace the page. */
    NSString *failingURL = [[error userInfo] objectForKey:NSURLErrorFailingURLStringErrorKey];
    NSString *mainURL = webView.request.URL.absoluteString;
    if (failingURL && mainURL && ![failingURL isEqualToString:mainURL]) {
        NSLog(@"[CGNATIVE] didFailLoad subresource failing=%@ main=%@", failingURL, mainURL);
        return;
    }
    /* Keep current page intact on unknown failures; only log for diagnostics. */
    if (!failingURL) return;
    if (mainURL && ![failingURL isEqualToString:mainURL]) return;

    NSString *html = [NSString stringWithFormat:
        @"<html><body style='background:#111;color:#ccc;font-family:monospace;padding:20px'>"
         "<p>%@</p></body></html>", error.localizedDescription];
    [webView loadHTMLString:html baseURL:nil];
}

- (BOOL)prefersStatusBarHidden { return NO; }
- (UIStatusBarStyle)preferredStatusBarStyle {
    /* UIStatusBarStyleLightContent is iOS 7+ — use default on older OS */
    if ([[UIApplication sharedApplication] respondsToSelector:@selector(setStatusBarStyle:animated:)])
        return (UIStatusBarStyle)1; /* UIStatusBarStyleLightContent = 1 */
    return UIStatusBarStyleDefault;
}

@end

#pragma mark - CGVoIP — native AudioQueue VoIP relay (UIWebView path)

/* Server-relayed audio: μ-law 8 kHz mono, 200 ms batches (1600 bytes / 1600 samples).
   We capture with AudioQueue (Linear PCM 16-bit mono 8 kHz), batch 1600 samples,
   μ-law-encode, POST to /api/calls/audio?call_id=N with X-CG-Token header.
   Playback: GET /api/calls/audio?call_id=N&after_seq=S every 200 ms, μ-law decode,
   enqueue into AudioQueue output. The whole thing runs on the main run loop using
   NSURLConnection async delegates so we work on iOS 3 (no GCD on iPhone 2G). */

#define CGVOIP_RATE        8000
#define CGVOIP_BATCH       1600           /* samples per ~200ms */
#define CGVOIP_NIN_BUFFERS 3
#define CGVOIP_NOUT_BUFFERS 4

static AudioQueueRef        gInQueue  = NULL;
static AudioQueueRef        gOutQueue = NULL;
static AudioQueueBufferRef  gInBufs[CGVOIP_NIN_BUFFERS];
static AudioQueueBufferRef  gOutBufs[CGVOIP_NOUT_BUFFERS];
static int64_t              gCallId   = 0;
static int64_t              gRxSeq    = 0;
static NSString            *gToken    = nil;
static NSString            *gBase     = nil;
static __weak UIWebView    *gWebView  = nil;
static NSTimer             *gPullTimer = nil;
static BOOL                 gActive   = NO;
static AVAudioPlayer       *gRingPlayer = nil;
static NSTimer             *gRingStopTimer = nil;
static int64_t              gTxPkts = 0;
static int64_t              gRxPkts = 0;
static int64_t              gDropPkts = 0;
static int64_t              gErrPkts = 0;
static int                  gLastRTTMs = 0;
static int                  gLastQueuePkts = 0;
static NSTimeInterval       gLastStatsEmitTs = 0;
static NSTimeInterval       gLastTxLogTs = 0;

static NSString *CGVoIPRingFilePath(void) {
    return [NSTemporaryDirectory() stringByAppendingPathComponent:@"cg_voip_ringtone.wav"];
}

static NSData *CGVoIPBuildRingWav(void) {
    const int sampleRate = 22050;
    const double duration = 2.4; /* two short rings + pause */
    const int frames = (int)(sampleRate * duration);
    const int dataSize = frames * 2;
    NSMutableData *d = [NSMutableData dataWithLength:(44 + dataSize)];
    unsigned char *b = (unsigned char *)[d mutableBytes];
    memset(b, 0, (44 + dataSize));
    memcpy(b + 0, "RIFF", 4);
    {
        UInt32 v = (UInt32)(36 + dataSize);
        b[4] = (unsigned char)(v & 0xff); b[5] = (unsigned char)((v >> 8) & 0xff);
        b[6] = (unsigned char)((v >> 16) & 0xff); b[7] = (unsigned char)((v >> 24) & 0xff);
    }
    memcpy(b + 8, "WAVEfmt ", 8);
    b[16] = 16; b[20] = 1; b[22] = 1;
    {
        UInt32 sr = (UInt32)sampleRate;
        UInt32 br = (UInt32)(sampleRate * 2);
        b[24] = (unsigned char)(sr & 0xff); b[25] = (unsigned char)((sr >> 8) & 0xff);
        b[26] = (unsigned char)((sr >> 16) & 0xff); b[27] = (unsigned char)((sr >> 24) & 0xff);
        b[28] = (unsigned char)(br & 0xff); b[29] = (unsigned char)((br >> 8) & 0xff);
        b[30] = (unsigned char)((br >> 16) & 0xff); b[31] = (unsigned char)((br >> 24) & 0xff);
    }
    b[32] = 2; b[34] = 16;
    memcpy(b + 36, "data", 4);
    {
        UInt32 ds = (UInt32)dataSize;
        b[40] = (unsigned char)(ds & 0xff); b[41] = (unsigned char)((ds >> 8) & 0xff);
        b[42] = (unsigned char)((ds >> 16) & 0xff); b[43] = (unsigned char)((ds >> 24) & 0xff);
    }
    {
        int i;
        const double pi = 3.14159265358979323846;
        for (i = 0; i < frames; i++) {
            double t = ((double)i) / ((double)sampleRate);
            double amp = 0.0;
            if (t < 0.28) {
                amp = sin(2.0 * pi * 730.0 * t);
            } else if (t >= 0.40 && t < 0.68) {
                amp = sin(2.0 * pi * 620.0 * t);
            }
            {
                short s = (short)(amp * 11000.0);
                int o = 44 + (i * 2);
                b[o] = (unsigned char)(s & 0xff);
                b[o + 1] = (unsigned char)((s >> 8) & 0xff);
            }
        }
    }
    return d;
}

static void CGVoIPEnsureRingPlayer(void) {
    if (gRingPlayer) return;
    @try {
        NSString *path = CGVoIPRingFilePath();
        if (![[NSFileManager defaultManager] fileExistsAtPath:path]) {
            NSData *wav = CGVoIPBuildRingWav();
            [wav writeToFile:path atomically:YES];
        }
        {
            NSError *err = nil;
            gRingPlayer = [[AVAudioPlayer alloc] initWithContentsOfURL:[NSURL fileURLWithPath:path] error:&err];
            if (!gRingPlayer || err) {
                NSLog(@"[CGVOIP] ring player init err=%@", err.localizedDescription);
                gRingPlayer = nil;
                return;
            }
            gRingPlayer.numberOfLoops = -1;
            gRingPlayer.volume = 0.9f;
            [gRingPlayer prepareToPlay];
        }
    } @catch (NSException *e) {
        NSLog(@"[CGVOIP] ring player ex=%@", e);
        gRingPlayer = nil;
    }
}

@interface CGVoIPRingStopTarget : NSObject
- (void)tick:(NSTimer *)t;
@end
@implementation CGVoIPRingStopTarget
- (void)tick:(NSTimer *)t { CGVoIPRingStop(); }
@end
static CGVoIPRingStopTarget *gRingStopTarget = nil;

static void CGVoIPRingStart(void) {
    if (gRingStopTimer) {
        [gRingStopTimer invalidate];
        gRingStopTimer = nil;
    }
    if (!gRingStopTarget) gRingStopTarget = [[CGVoIPRingStopTarget alloc] init];
    CGVoIPEnsureRingPlayer();
    if (gRingPlayer) {
        if (![gRingPlayer isPlaying]) {
            gRingPlayer.currentTime = 0;
            [gRingPlayer play];
        }
        gRingStopTimer = [NSTimer scheduledTimerWithTimeInterval:45.0
                                                          target:gRingStopTarget
                                                        selector:@selector(tick:)
                                                        userInfo:nil
                                                         repeats:NO];
        return;
    }
    AudioServicesPlaySystemSound(1003);
    gRingStopTimer = [NSTimer scheduledTimerWithTimeInterval:45.0
                                                      target:gRingStopTarget
                                                    selector:@selector(tick:)
                                                    userInfo:nil
                                                     repeats:NO];
}

static void CGVoIPRingStop(void) {
    if (gRingStopTimer) {
        [gRingStopTimer invalidate];
        gRingStopTimer = nil;
    }
    if (gRingPlayer) {
        [gRingPlayer stop];
        gRingPlayer.currentTime = 0;
    }
}

static void CGVoIPSetSpeakerEnabled(BOOL enabled) {
    @try {
        AVAudioSession *sess = [AVAudioSession sharedInstance];
        NSError *err = nil;
        if ([sess respondsToSelector:@selector(overrideOutputAudioPort:error:)]) {
            [sess overrideOutputAudioPort:(enabled ? AVAudioSessionPortOverrideSpeaker : AVAudioSessionPortOverrideNone)
                                     error:&err];
            if (err) NSLog(@"[CGVOIP] override speaker err=%@", err.localizedDescription);
        } else {
            UInt32 v = enabled ? 1 : 0;
            OSStatus s = AudioSessionSetProperty(kAudioSessionProperty_OverrideCategoryDefaultToSpeaker,
                                                 sizeof(v), &v);
            if (s != noErr) NSLog(@"[CGVOIP] legacy speaker set err=%d", (int)s);
        }
    } @catch (NSException *e) {
        NSLog(@"[CGVOIP] speaker ex=%@", e);
    }
}

static void CGVoIPEmitStats(void) {
    NSTimeInterval now = [[NSDate date] timeIntervalSince1970];
    if (now - gLastStatsEmitTs < 1.0) return;
    gLastStatsEmitTs = now;
    NSString *m = [NSString stringWithFormat:@"rtt=%d;tx=%lld;rx=%lld;drop=%lld;q=%d;err=%lld",
                   gLastRTTMs, (long long)gTxPkts, (long long)gRxPkts,
                   (long long)gDropPkts, gLastQueuePkts, (long long)gErrPkts];
    CGVoIPNotify(@"stats", m);
}

/* μ-law (G.711) codec */
static unsigned char CGLin2Ulaw(int16_t pcm) {
    const int16_t CLIP = 32635, BIAS = 0x84;
    int sign = (pcm < 0) ? 0x80 : 0;
    if (sign) pcm = -pcm;
    if (pcm > CLIP) pcm = CLIP;
    pcm += BIAS;
    int seg = 7, mask = 0x4000;
    while ((pcm & mask) == 0 && seg > 0) { mask >>= 1; seg--; }
    int mant = (pcm >> (seg + 3)) & 0x0F;
    return (unsigned char)(~(sign | (seg << 4) | mant)) & 0xFF;
}
static int16_t CGUlaw2Lin(unsigned char u) {
    u = (~u) & 0xFF;
    int sign = u & 0x80;
    int seg  = (u >> 4) & 0x07;
    int mant =  u       & 0x0F;
    int pcm  = ((mant << 3) + 0x84) << seg;
    pcm -= 0x84;
    return (int16_t)(sign ? -pcm : pcm);
}

static NSDictionary *CGParseQueryString(NSString *q) {
    NSMutableDictionary *out = [NSMutableDictionary dictionary];
    if (!q.length) return out;
    NSArray *pairs = [q componentsSeparatedByString:@"&"];
    for (NSString *p in pairs) {
        NSRange eq = [p rangeOfString:@"="];
        if (eq.location == NSNotFound) continue;
        NSString *k = [p substringToIndex:eq.location];
        NSString *vEnc = [p substringFromIndex:eq.location + 1];
        NSString *v = [vEnc stringByReplacingPercentEscapesUsingEncoding:NSUTF8StringEncoding] ?: @"";
        if (k.length) [out setObject:v forKey:k];
    }
    return out;
}

@interface CGVoIPNotifyTarget : NSObject
- (void)notify:(NSArray *)args;
@end
@implementation CGVoIPNotifyTarget
- (void)notify:(NSArray *)args {
    UIWebView *wv = gWebView;
    if (!wv) return;
    NSString *state = ([args count] > 0 ? [args objectAtIndex:0] : @"");
    NSString *msg = ([args count] > 1 ? [args objectAtIndex:1] : @"");
    NSString *escState = [state stringByReplacingOccurrencesOfString:@"'" withString:@"\\'"];
    NSString *escMsg   = [msg stringByReplacingOccurrencesOfString:@"'" withString:@"\\'"];
    NSString *js = [NSString stringWithFormat:
        @"try{if(window.cgVoIPState)window.cgVoIPState('%@','%@');}catch(e){}",
        escState, escMsg];
    [wv stringByEvaluatingJavaScriptFromString:js];
}
@end
static CGVoIPNotifyTarget *gNotifyTarget = nil;

static void CGVoIPNotify(NSString *state, NSString *msg) {
    if (!gNotifyTarget) gNotifyTarget = [[CGVoIPNotifyTarget alloc] init];
    NSArray *args = [NSArray arrayWithObjects:(state ?: @""), (msg ?: @""), nil];
    if ([NSThread isMainThread]) {
        [gNotifyTarget notify:args];
    } else {
        [gNotifyTarget performSelectorOnMainThread:@selector(notify:)
                                        withObject:args
                                     waitUntilDone:NO];
    }
}

@interface CGVoIPPushTarget : NSObject
- (void)sendChunk:(NSData *)out;
@end
@implementation CGVoIPPushTarget
- (void)sendChunk:(NSData *)out {
    if (!gActive || gCallId == 0 || !out.length) return;
    {
        NSTimeInterval now = [[NSDate date] timeIntervalSince1970];
        if (now - gLastTxLogTs >= 2.0) {
            gLastTxLogTs = now;
            NSLog(@"[CGVOIP] tx chunk bytes=%u call=%lld", (unsigned int)[out length], (long long)gCallId);
        }
    }
    NSString *urlStr = [NSString stringWithFormat:@"%@/api/calls/audio?call_id=%lld",
                        gBase, (long long)gCallId];
    NSMutableURLRequest *req = [NSMutableURLRequest requestWithURL:[NSURL URLWithString:urlStr]];
    [req setHTTPMethod:@"POST"];
    [req setValue:@"application/octet-stream" forHTTPHeaderField:@"Content-Type"];
    [req setValue:gToken forHTTPHeaderField:@"X-CG-Token"];
    [req setHTTPBody:out];
    [req setTimeoutInterval:5.0];
    NSURLConnection *c = [[NSURLConnection alloc] initWithRequest:req delegate:nil startImmediately:NO];
    [c scheduleInRunLoop:[NSRunLoop mainRunLoop] forMode:NSDefaultRunLoopMode];
    [c start];
}
@end
static CGVoIPPushTarget *gPushTarget = nil;

static void CGVoIPInputCallback(void *inUserData,
                                AudioQueueRef inAQ,
                                AudioQueueBufferRef inBuffer,
                                const AudioTimeStamp *inStartTime,
                                UInt32 inNumberPacketDescriptions,
                                const AudioStreamPacketDescription *inPacketDescs) {
    if (!gActive || gCallId == 0) {
        AudioQueueEnqueueBuffer(inAQ, inBuffer, 0, NULL);
        return;
    }
    if (gMuted) {
        AudioQueueEnqueueBuffer(inAQ, inBuffer, 0, NULL);
        return;
    }
    int16_t *pcm = (int16_t *)inBuffer->mAudioData;
    UInt32 nSamples = inBuffer->mAudioDataByteSize / sizeof(int16_t);
     @autoreleasepool {
          /* Encode μ-law into a fresh NSData, then hand the upload off to the
              main thread. iOS 6/armv7s was crashing when Foundation networking
              objects were created directly from the AudioQueue input callback. */
          NSMutableData *out = [NSMutableData dataWithLength:nSamples];
          unsigned char *dst = (unsigned char *)out.mutableBytes;
          for (UInt32 i = 0; i < nSamples; i++) dst[i] = CGLin2Ulaw(pcm[i]);
          if (!gPushTarget) gPushTarget = [[CGVoIPPushTarget alloc] init];
          [gPushTarget performSelectorOnMainThread:@selector(sendChunk:)
                                                  withObject:out
                                              waitUntilDone:NO];
     }
    gTxPkts += (nSamples / CGVOIP_BATCH) + ((nSamples % CGVOIP_BATCH) ? 1 : 0);
    CGVoIPEmitStats();

    AudioQueueEnqueueBuffer(inAQ, inBuffer, 0, NULL);
}

static void CGVoIPOutputCallback(void *inUserData,
                                 AudioQueueRef inAQ,
                                 AudioQueueBufferRef inBuffer) {
    /* Output buffer became free — fill with silence and re-enqueue. The pull
       timer below copies real audio in via AudioQueueEnqueueBuffer of *new*
       buffers allocated against the queue. To keep latency low we just keep
       this buffer recycled with silence so the queue stays primed. */
    if (!gActive) return;
    memset(inBuffer->mAudioData, 0, inBuffer->mAudioDataByteSize);
    inBuffer->mAudioDataByteSize = inBuffer->mAudioDataBytesCapacity;
    AudioQueueEnqueueBuffer(inAQ, inBuffer, 0, NULL);
}

@interface CGVoIPPullDelegate : NSObject {
    NSMutableData *_responseData;
    NSHTTPURLResponse *_response;
    NSTimeInterval _startedAt;
}
@property (nonatomic, strong) NSMutableData *responseData;
@property (nonatomic, strong) NSHTTPURLResponse *response;
@property (nonatomic, assign) NSTimeInterval startedAt;
@end
@implementation CGVoIPPullDelegate
@synthesize responseData = _responseData;
@synthesize response = _response;
@synthesize startedAt = _startedAt;

- (void)connection:(NSURLConnection *)c didReceiveResponse:(NSURLResponse *)r {
    if ([r isKindOfClass:[NSHTTPURLResponse class]]) self.response = (NSHTTPURLResponse *)r;
    self.responseData = [NSMutableData data];
}
- (void)connection:(NSURLConnection *)c didReceiveData:(NSData *)d {
    [self.responseData appendData:d];
}
- (void)connection:(NSURLConnection *)c didFailWithError:(NSError *)e {
    NSLog(@"[CGVOIP] pull err=%@", e.localizedDescription);
    gErrPkts++;
    CGVoIPEmitStats();
}
- (void)connectionDidFinishLoading:(NSURLConnection *)c {
    if (!gActive || !gOutQueue) return;
    gLastRTTMs = (int)(([[NSDate date] timeIntervalSince1970] - self.startedAt) * 1000.0);
    int64_t prevSeq = gRxSeq;
    NSString *lastSeqStr = [self.response.allHeaderFields objectForKey:@"X-CG-Last-Seq"];
    if (!lastSeqStr) lastSeqStr = [self.response.allHeaderFields objectForKey:@"x-cg-last-seq"];
    if (lastSeqStr) {
        int64_t s = (int64_t)[lastSeqStr longLongValue];
        if (prevSeq > 0 && s > prevSeq + 1) gDropPkts += (s - prevSeq - 1);
        if (s > gRxSeq) gRxSeq = s;
    }
    NSData *body = self.responseData;
    gLastQueuePkts = (int)((body.length + CGVOIP_BATCH - 1) / CGVOIP_BATCH);
    if (!body.length) return;
    /* Decode μ-law → 16-bit PCM and enqueue a fresh AudioQueueBuffer. */
    const unsigned char *src = body.bytes;
    NSUInteger n = body.length;
    AudioQueueBufferRef buf = NULL;
    OSStatus s = AudioQueueAllocateBuffer(gOutQueue, (UInt32)(n * sizeof(int16_t)), &buf);
    if (s != noErr || !buf) return;
    int16_t *dst = (int16_t *)buf->mAudioData;
    for (NSUInteger i = 0; i < n; i++) dst[i] = CGUlaw2Lin(src[i]);
    buf->mAudioDataByteSize = (UInt32)(n * sizeof(int16_t));
    AudioQueueEnqueueBuffer(gOutQueue, buf, 0, NULL);
    gRxPkts += (n / CGVOIP_BATCH) + ((n % CGVOIP_BATCH) ? 1 : 0);
    CGVoIPEmitStats();
}
@end

static CGVoIPPullDelegate *gPullDel = nil;

static void CGVoIPPullTick(NSTimer *timer);

@interface CGVoIPTimerTarget : NSObject
- (void)tick:(NSTimer *)t;
@end
@implementation CGVoIPTimerTarget
- (void)tick:(NSTimer *)t { CGVoIPPullTick(t); }
@end
static CGVoIPTimerTarget *gTimerTarget = nil;

static void CGVoIPPullTick(NSTimer *timer) {
    if (!gActive || gCallId == 0) return;
    NSString *urlStr = [NSString stringWithFormat:@"%@/api/calls/audio?call_id=%lld&after_seq=%lld",
                        gBase, (long long)gCallId, (long long)gRxSeq];
    NSMutableURLRequest *req = [NSMutableURLRequest requestWithURL:[NSURL URLWithString:urlStr]];
    [req setValue:gToken forHTTPHeaderField:@"X-CG-Token"];
    [req setTimeoutInterval:4.0];
    if (!gPullDel) gPullDel = [[CGVoIPPullDelegate alloc] init];
    /* New delegate per request to avoid response-buffer races. */
    CGVoIPPullDelegate *del = [[CGVoIPPullDelegate alloc] init];
    del.startedAt = [[NSDate date] timeIntervalSince1970];
    NSURLConnection *c = [[NSURLConnection alloc] initWithRequest:req delegate:del startImmediately:NO];
    [c scheduleInRunLoop:[NSRunLoop mainRunLoop] forMode:NSDefaultRunLoopMode];
    [c start];
    /* Retain delegate by attaching via objc_setAssociatedObject would be cleaner;
       simplest: store in a static, since calls overlap is fine on slow links. */
    gPullDel = del;
}

static void CGVoIPStart(int64_t callId, NSString *token, NSString *base, UIWebView *webView) {
    if (gActive) {
        NSLog(@"[CGVOIP] start ignored, already active call=%lld", (long long)gCallId);
        return;
    }
    CGVoIPRingStop();
    gCallId  = callId;
    gToken   = [token copy];
    gBase    = [base copy];
    gWebView = webView;
    gRxSeq   = 0;
    gActive  = YES;
    gMuted   = NO;
    gTxPkts = gRxPkts = gDropPkts = gErrPkts = 0;
    gLastRTTMs = 0;
    gLastQueuePkts = 0;
    gLastStatsEmitTs = 0;
    gLastTxLogTs = 0;
    NSLog(@"[CGVOIP] start call=%lld base=%@", (long long)callId, base);

    /* Configure AVAudioSession for play+record (iOS 3+ supported). */
    @try {
        AVAudioSession *sess = [AVAudioSession sharedInstance];
        NSError *err = nil;
        [sess setCategory:AVAudioSessionCategoryPlayAndRecord error:&err];
        if (err) NSLog(@"[CGVOIP] setCategory err=%@", err.localizedDescription);
        err = nil;
        [sess setActive:YES error:&err];
        if (err) NSLog(@"[CGVOIP] activate err=%@", err.localizedDescription);
        CGVoIPSetSpeakerEnabled(YES);
    } @catch (NSException *e) { NSLog(@"[CGVOIP] session ex=%@", e); }

    AudioStreamBasicDescription fmt = {0};
    fmt.mSampleRate       = CGVOIP_RATE;
    fmt.mFormatID         = kAudioFormatLinearPCM;
    fmt.mFormatFlags      = kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked;
    fmt.mChannelsPerFrame = 1;
    fmt.mBitsPerChannel   = 16;
    fmt.mFramesPerPacket  = 1;
    fmt.mBytesPerFrame    = 2;
    fmt.mBytesPerPacket   = 2;

    /* Input queue */
    OSStatus s = AudioQueueNewInput(&fmt, CGVoIPInputCallback, NULL, NULL, NULL, 0, &gInQueue);
    if (s != noErr) {
        NSLog(@"[CGVOIP] AudioQueueNewInput err=%d", (int)s);
        CGVoIPNotify(@"error", @"input_open_failed");
        CGVoIPStop();
        return;
    }
    UInt32 inBufBytes = CGVOIP_BATCH * sizeof(int16_t);
    for (int i = 0; i < CGVOIP_NIN_BUFFERS; i++) {
        AudioQueueAllocateBuffer(gInQueue, inBufBytes, &gInBufs[i]);
        AudioQueueEnqueueBuffer(gInQueue, gInBufs[i], 0, NULL);
    }
    s = AudioQueueStart(gInQueue, NULL);
    if (s != noErr) {
        NSLog(@"[CGVOIP] AudioQueueStart input err=%d", (int)s);
        CGVoIPNotify(@"error", @"input_start_failed");
        CGVoIPStop();
        return;
    }

    /* Output queue */
    s = AudioQueueNewOutput(&fmt, CGVoIPOutputCallback, NULL, NULL, NULL, 0, &gOutQueue);
    if (s != noErr) {
        NSLog(@"[CGVOIP] AudioQueueNewOutput err=%d", (int)s);
        CGVoIPNotify(@"error", @"output_open_failed");
        CGVoIPStop();
        return;
    }
    /* Pre-fill with silence so the queue can start. */
    for (int i = 0; i < CGVOIP_NOUT_BUFFERS; i++) {
        AudioQueueAllocateBuffer(gOutQueue, inBufBytes, &gOutBufs[i]);
        memset(gOutBufs[i]->mAudioData, 0, inBufBytes);
        gOutBufs[i]->mAudioDataByteSize = inBufBytes;
        AudioQueueEnqueueBuffer(gOutQueue, gOutBufs[i], 0, NULL);
    }
    s = AudioQueueStart(gOutQueue, NULL);
    if (s != noErr) {
        NSLog(@"[CGVOIP] AudioQueueStart output err=%d", (int)s);
        CGVoIPNotify(@"error", @"output_start_failed");
        CGVoIPStop();
        return;
    }

    /* Pull timer at 200 ms */
    if (!gTimerTarget) gTimerTarget = [[CGVoIPTimerTarget alloc] init];
    gPullTimer = [NSTimer scheduledTimerWithTimeInterval:0.2
                                                  target:gTimerTarget
                                                selector:@selector(tick:)
                                                userInfo:nil
                                                 repeats:YES];
    NSLog(@"[CGVOIP] running");
}

static void CGVoIPStop(void) {
    if (!gActive) return;
    gActive = NO;
    CGVoIPRingStop();
    CGVoIPSetSpeakerEnabled(NO);
    NSLog(@"[CGVOIP] stop call=%lld", (long long)gCallId);
    if (gPullTimer) { [gPullTimer invalidate]; gPullTimer = nil; }
    if (gInQueue) {
        AudioQueueStop(gInQueue, true);
        AudioQueueDispose(gInQueue, true);
        gInQueue = NULL;
    }
    if (gOutQueue) {
        AudioQueueStop(gOutQueue, true);
        AudioQueueDispose(gOutQueue, true);
        gOutQueue = NULL;
    }
    @try {
        NSError *err = nil;
        [[AVAudioSession sharedInstance] setActive:NO error:&err];
    } @catch (NSException *e) {}
    gCallId = 0;
    gRxSeq  = 0;
    gToken  = nil;
    gBase   = nil;
    gMuted  = NO;
}
