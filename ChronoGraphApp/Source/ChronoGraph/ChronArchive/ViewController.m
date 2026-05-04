#import "ViewController.h"

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

- (UIColor *)accentColorForName:(NSString *)name {
    if (!name) return [UIColor colorWithRed:0.35 green:0.78 blue:0.98 alpha:1.0];
    if ([name isEqualToString:@"green"]) {
        return [UIColor colorWithRed:0.39 green:0.85 blue:0.20 alpha:1.0];
    }
    return [UIColor colorWithRed:0.35 green:0.78 blue:0.98 alpha:1.0];
}

- (void)webViewDidFinishLoad:(UIWebView *)webView {
    NSLog(@"[CGNATIVE] didFinishLoad req=%@", webView.request.URL.absoluteString);

    [webView stringByEvaluatingJavaScriptFromString:@"window.__cgNativeImagePicker=1;"];

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
                            "if(feed && ((!feed.innerHTML)||feed.innerHTML.length<8) && typeof loadFeed==='function'){"
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
    }

    /* about: and javascript: — always allow (inline handlers, about:blank etc.) */
    if ([scheme isEqualToString:@"about"] ||
        [scheme isEqualToString:@"javascript"]) return YES;

    /* cgopen:// — JS-side explicit external URL open (search results, web search fallback).
       The URL to open is the encoded URL-path/query of the cgopen:// request.
       e.g.  window.location.href = 'cgopen://open?u=' + encodeURIComponent(targetURL); */
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
            NSURL *extURL = [NSURL URLWithString:target];
            if (extURL && [[UIApplication sharedApplication] canOpenURL:extURL]) {
                [[UIApplication sharedApplication] openURL:extURL];
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
    CGFloat maxEdge = isAvatar ? 256.0f : 1600.0f;
    NSString *callback = isAvatar ? @"__cgNativeAvatarPicked" : @"__cgNativePostPicked";
    UIImage *scaled = [self scaledImageForAvatar:img maxEdge:maxEdge];
    NSData *jpeg = UIImageJPEGRepresentation(scaled, 0.80f);
    NSString *b64 = CGBase64FromData(jpeg);
    if (b64 && [b64 length] > 0) {
        NSString *dataURL = [NSString stringWithFormat:@"data:image/jpeg;base64,%@", b64];
        NSString *escaped = [dataURL stringByReplacingOccurrencesOfString:@"\\" withString:@"\\\\"];
        escaped = [escaped stringByReplacingOccurrencesOfString:@"'" withString:@"\\'"];
        NSString *js = [NSString stringWithFormat:@"(function(){if(window.%@){window.%@('%@');}})();", callback, callback, escaped];
        [self.webView stringByEvaluatingJavaScriptFromString:js];
        NSLog(@"[CGNATIVE] %@Picker success bytes=%lu", isAvatar ? @"avatar" : @"post", (unsigned long)[jpeg length]);
    } else {
        NSLog(@"[CGNATIVE] %@Picker failed encode", isAvatar ? @"avatar" : @"post");
    }
    self.imagePickerTarget = nil;
    if ([picker respondsToSelector:@selector(dismissViewControllerAnimated:completion:)]) {
        [picker dismissViewControllerAnimated:YES completion:nil];
    } else {
        [picker dismissModalViewControllerAnimated:YES];
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
