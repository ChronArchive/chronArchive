#import "ViewController.h"

@interface ViewController ()
/* On armv6 builds we compile without ARC, so object properties must retain. */
#if __has_feature(objc_arc)
@property (nonatomic, strong) UIWebView *webView;
@property (nonatomic, strong) NSURL *initialURL;  /* page this VC loads */
@property (nonatomic, strong) NSURL *rootURL;     /* tab root — nav bar hidden when here */
#else
@property (nonatomic, retain) UIWebView *webView;
@property (nonatomic, retain) NSURL *initialURL;  /* page this VC loads */
@property (nonatomic, retain) NSURL *rootURL;     /* tab root — nav bar hidden when here */
#endif
@end

@implementation ViewController
@synthesize webView = _webView;
@synthesize initialURL = _initialURL;
@synthesize rootURL = _rootURL;

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
    NSInteger legacyMajor = (NSInteger)[[[UIDevice currentDevice] systemVersion] intValue];

    /* iOS 7+: stop UIKit from pushing content down under the hidden nav bar.
       Cast to id and use raw integer values to avoid iOS 5 SDK type errors. */
    if ([self respondsToSelector:@selector(setEdgesForExtendedLayout:)])
        [(id)self setValue:[NSNumber numberWithInt:0] forKey:@"edgesForExtendedLayout"];
    if ([self respondsToSelector:@selector(setAutomaticallyAdjustsScrollViewInsets:)])
        [(id)self setValue:[NSNumber numberWithBool:NO] forKey:@"automaticallyAdjustsScrollViewInsets"];

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

/* Hide the native nav bar on the tab root page; show it on any sub-page so the
   back button is visible.  Called on appear so it also fires correctly after a pop. */
- (void)viewWillAppear:(BOOL)animated {
    [super viewWillAppear:animated];
    BOOL isRoot = [self.initialURL.absoluteString isEqualToString:self.rootURL.absoluteString];
    NSLog(@"[CGNATIVE] viewWillAppear url=%@ isRoot=%d", self.initialURL.absoluteString, isRoot ? 1 : 0);
    [self.navigationController setNavigationBarHidden:isRoot animated:animated];
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
        [webView stringByEvaluatingJavaScriptFromString:
                @"(function(){"
                 "if(window.__cgConsoleBridgeInstalled) return;"
                 "window.__cgConsoleBridgeInstalled=1;"
                 "function cgSend(level,msg){"
                     "try{"
                         "var s=''+msg;"
                         "if(s.length>700) s=s.substring(0,700);"
                         "var u='cglog://'+encodeURIComponent(level+': '+s);"
                         "var ifr=document.createElement('iframe');"
                         "ifr.style.display='none';"
                         "ifr.src=u;"
                         "(document.documentElement||document.body).appendChild(ifr);"
                         "setTimeout(function(){try{ifr.parentNode.removeChild(ifr);}catch(e){}},0);"
                     "}catch(e){}"
                 "}"
                 "if(!window.console) window.console={};"
                 "var oldLog=window.console.log;"
                 "window.console.log=function(){"
                     "try{var a=[];for(var i=0;i<arguments.length;i++)a.push(arguments[i]);cgSend('log',a.join(' '));}catch(e){}"
                     "try{if(oldLog) oldLog.apply(window.console,arguments);}catch(e2){}"
                 "};"
                 "var oldErr=window.console.error;"
                 "window.console.error=function(){"
                     "try{var a=[];for(var i=0;i<arguments.length;i++)a.push(arguments[i]);cgSend('error',a.join(' '));}catch(e){}"
                     "try{if(oldErr) oldErr.apply(window.console,arguments);}catch(e2){}"
                 "};"
                 "window.onerror=function(msg,url,line){"
                     "cgSend('js',''+msg+' @'+url+':'+line);"
                     "return false;"
                 "};"
                "})();"];

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

        NSString *legacyForce = [webView stringByEvaluatingJavaScriptFromString:
                @"(function(){"
                    "try{"
                        "if(!/OS [1-4]_/.test(navigator.userAgent||'')) return 'skip:not-legacy';"
                        "var href=(window.location&&window.location.href)?window.location.href:'';"
                        "var s=document.getElementById('__cg-legacy-force-style');"
                        "if(!s){s=document.createElement('style');s.id='__cg-legacy-force-style';(document.head||document.documentElement).appendChild(s);}"
                        "var css='html,body{background:#f2f2f7 !important;color:#1a1a1a !important;opacity:1 !important;visibility:visible !important;}';"
                        "if(href.indexOf('/pages/home.html')>=0){"
                            "css+='#posts-feed,.post-card,.post-hd,.post-body,.post-actions{opacity:1 !important;visibility:visible !important;}';"
                        "}"
                        "if(href.indexOf('/pages/search.html')>=0){"
                            "css+='#results-body,.res-post,.res-person,.res-web,.sug-row{opacity:1 !important;visibility:visible !important;}';"
                        "}"
                        "if(href.indexOf('/pages/account.html')>=0){"
                            "css+='#view-signedin,#view-signedout{opacity:1 !important;visibility:visible !important;}';"
                        "}"
                        "if(href.indexOf('/pages/chat.html')>=0){"
                            "css+='.screen.active{display:block !important;opacity:1 !important;visibility:visible !important;}';"
                        "}"
                        "s.innerHTML=css;"
                        "return 'applied';"
                    "}catch(e){return 'err='+e;}"
                "})();"];
        NSLog(@"[CGNATIVE] didFinishLegacyForce %@", legacyForce);

        NSString *legacyScrollNormalize = [webView stringByEvaluatingJavaScriptFromString:
                @"(function(){"
                    "try{"
                        "if(!/OS [1-4]_/.test(navigator.userAgent||'')) return 'skip:not-legacy';"
                        "var href=(window.location&&window.location.href)?window.location.href:'';"
                        "if(href.indexOf('/pages/tools.html')>=0) return 'skip:tools';"
                        "var s=document.getElementById('__cg-legacy-native-scroll-style');"
                        "if(!s){s=document.createElement('style');s.id='__cg-legacy-native-scroll-style';(document.head||document.documentElement).appendChild(s);}"
                        "var css='html,body{height:auto !important;min-height:100% !important;overflow:auto !important;-webkit-overflow-scrolling:touch !important;}';"
                        "if(href.indexOf('/pages/home.html')>=0){"
                            "css+='#shell{display:block !important;height:auto !important;overflow:visible !important;position:relative !important;}';"
                            "css+='#hdr{position:fixed !important;top:0 !important;left:0 !important;right:0 !important;z-index:100 !important;}';"
                            "css+='#feed-wrap{position:relative !important;top:0 !important;bottom:auto !important;height:auto !important;overflow:visible !important;padding-top:44px !important;}';"
                        "}"
                        "if(href.indexOf('/pages/search.html')>=0){"
                            "css+='#shell{display:block !important;height:auto !important;overflow:visible !important;position:relative !important;}';"
                            "css+='#searchbar,#cat-tabs{position:fixed !important;left:0 !important;right:0 !important;z-index:101 !important;}';"
                            "css+='#results-wrap{position:relative !important;top:0 !important;bottom:auto !important;height:auto !important;overflow:visible !important;padding-top:80px !important;}';"
                        "}"
                        "if(href.indexOf('/pages/account.html')>=0){"
                            "css+='#hdr{position:fixed !important;top:0 !important;left:0 !important;right:0 !important;z-index:100 !important;}';"
                            "css+='#scroll{position:relative !important;top:0 !important;bottom:auto !important;height:auto !important;overflow:visible !important;padding-top:44px !important;}';"
                            "css+='.subscreen{position:relative !important;top:0 !important;bottom:auto !important;height:auto !important;}';"
                            "css+='.sub-hdr{position:fixed !important;top:0 !important;left:0 !important;right:0 !important;z-index:201 !important;}';"
                            "css+='.sub-body{position:relative !important;top:0 !important;bottom:auto !important;height:auto !important;overflow:visible !important;padding-top:44px !important;}';"
                        "}"
                        "if(href.indexOf('/pages/chat.html')>=0){"
                            "css+='.screen{position:relative !important;top:0 !important;bottom:auto !important;height:auto !important;min-height:100% !important;}';"
                            "css+='.screen.active{display:block !important;}';"
                            "css+='.header{position:fixed !important;top:0 !important;left:0 !important;right:0 !important;z-index:100 !important;}';"
                            "css+='.sb{position:relative !important;top:0 !important;bottom:auto !important;height:auto !important;overflow:visible !important;padding-top:44px !important;}';"
                            "css+='#auth-screen .sb{padding-top:0 !important;}';"
                            "css+='.ibar{position:fixed !important;left:0 !important;right:0 !important;bottom:0 !important;z-index:101 !important;}';"
                            "css+='#thread-screen{padding-bottom:50px !important;}';"
                        "}"
                        "s.innerHTML=css;"
                        "setTimeout(function(){"
                            "try{"
                                "window.scrollTo(0,1);window.scrollTo(0,0);"
                                "var de=document.documentElement,db=document.body;"
                                "var sh=Math.max(de?de.scrollHeight:0,db?db.scrollHeight:0);"
                                "if(window.console&&console.log)console.log('[CG][LEGACY_SCROLL] href='+href+' sh='+sh+' wh='+(window.innerHeight||0));"
                            "}catch(e2){}"
                        "},90);"
                        "return 'applied';"
                    "}catch(e){return 'err='+e;}"
                "})();"];
        NSLog(@"[CGNATIVE] didFinishLegacyScrollNormalize %@", legacyScrollNormalize);

        NSString *rescue = [webView stringByEvaluatingJavaScriptFromString:
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
                            "if(!active){"
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

    NSInteger legacyMajor = (NSInteger)[[[UIDevice currentDevice] systemVersion] intValue];
    NSString *accentName = nil;
    if (legacyMajor > 0 && legacyMajor <= 4) {
        accentName = @"blue";
    } else {
        accentName = [webView stringByEvaluatingJavaScriptFromString:
            @"(function(){var a=localStorage.getItem('cg-accent-color'); if(!a)a=localStorage.getItem('cg-chat-bubble'); if(!a)a='blue'; return a; })();"];
    }
    if (accentName && accentName.length > 0) {
        UIColor *accent = [self accentColorForName:accentName];
        self.navigationController.navigationBar.tintColor = accent;
        /* Keep tab bar icons fully original/opaque: do not set tabBar.tintColor here. */
    }
}

- (void)didReceiveMemoryWarning {
    [super didReceiveMemoryWarning];
    /* Free the WebView when this tab is not on screen to reclaim memory.
       viewDidLoad re-creates it and reloads from initialURL next time the tab is shown. */
    if (self.isViewLoaded && self.view.window == nil) {
        if (self.webView) {
            /* Remember where the user was so we can restore on next selection */
            NSURL *cur = self.webView.request.URL;
            if (cur) self.initialURL = cur;
            self.webView.delegate = nil;
            [self.webView stopLoading];
            [self.webView removeFromSuperview];
            self.webView = nil;
        }
        self.view = nil; /* causes viewDidLoad to be called again when tab is re-selected */
    }
}

- (void)dealloc {
    self.webView.delegate = nil;
#if !__has_feature(objc_arc)
    [_webView release];
    [_initialURL release];
    [_rootURL release];
    [super dealloc];
#endif
}

#pragma mark - UIWebViewDelegate

- (BOOL)webView:(UIWebView *)webView shouldStartLoadWithRequest:(NSURLRequest *)request
                                                 navigationType:(UIWebViewNavigationType)navigationType {
    NSURL      *url    = request.URL;
    NSString   *scheme = url.scheme.lowercaseString;

    if ([scheme isEqualToString:@"cglog"]) {
        NSString *msg = [url resourceSpecifier] ?: @"";
        msg = [msg stringByReplacingOccurrencesOfString:@"//" withString:@""];
        msg = [msg stringByReplacingPercentEscapesUsingEncoding:NSUTF8StringEncoding] ?: msg;
        NSLog(@"[CGJS] %@", msg);
        return NO;
    }

    /* about: and javascript: — always allow (inline handlers, about:blank etc.) */
    if ([scheme isEqualToString:@"about"] ||
        [scheme isEqualToString:@"javascript"]) return YES;

    /* Non-http(s) / non-file schemes (itms-services:, mailto:, tel:, etc.) — hand to system */
    if (![scheme isEqualToString:@"file"] &&
        ![scheme isEqualToString:@"http"]  &&
        ![scheme isEqualToString:@"https"]) {
        if ([[UIApplication sharedApplication] canOpenURL:url]) {
            [[UIApplication sharedApplication] openURL:url];
        }
        return NO;
    }

    /* User-initiated navigations (link tap or form submit) open in a pushed VC so the
       native back button works.  Sub-resource loads (images, scripts, XHR) fall through. */
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

- (BOOL)prefersStatusBarHidden { return YES; }
- (UIStatusBarStyle)preferredStatusBarStyle {
    /* UIStatusBarStyleLightContent is iOS 7+ — use default on older OS */
    if ([[UIApplication sharedApplication] respondsToSelector:@selector(setStatusBarStyle:animated:)])
        return (UIStatusBarStyle)1; /* UIStatusBarStyleLightContent = 1 */
    return UIStatusBarStyleDefault;
}

@end
