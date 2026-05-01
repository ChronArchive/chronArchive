#import "ViewController.h"

@interface ViewController ()
@property (nonatomic, strong) UIWebView *webView;
@property (nonatomic, strong) NSURL *initialURL;  /* page this VC loads */
@property (nonatomic, strong) NSURL *rootURL;     /* tab root — nav bar hidden when here */
@end

@implementation ViewController
@synthesize webView = _webView;
@synthesize initialURL = _initialURL;
@synthesize rootURL = _rootURL;

- (instancetype)initWithURL:(NSURL *)url rootURL:(NSURL *)rootURL {
    self = [super init];
    if (self) {
        _initialURL = url;
        _rootURL    = rootURL;
    }
    return self;
}

- (void)viewDidLoad {
    [super viewDidLoad];
    self.view.backgroundColor = [UIColor blackColor];

    /* iOS 7+: stop UIKit from pushing content down under the hidden nav bar.
       Cast to id and use raw integer values to avoid iOS 5 SDK type errors. */
    if ([self respondsToSelector:@selector(setEdgesForExtendedLayout:)])
        [(id)self setValue:[NSNumber numberWithInt:0] forKey:@"edgesForExtendedLayout"];
    if ([self respondsToSelector:@selector(setAutomaticallyAdjustsScrollViewInsets:)])
        [(id)self setValue:[NSNumber numberWithBool:NO] forKey:@"automaticallyAdjustsScrollViewInsets"];

    self.webView = [[UIWebView alloc] initWithFrame:self.view.bounds];
    self.webView.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
    self.webView.delegate = self;
    self.webView.scalesPageToFit = YES;
    /* mediaPlaybackRequiresUserAction and allowsInlineMediaPlayback are iOS 4+ */
    if ([self.webView respondsToSelector:@selector(setMediaPlaybackRequiresUserAction:)])
        self.webView.mediaPlaybackRequiresUserAction = NO;
    if ([self.webView respondsToSelector:@selector(setAllowsInlineMediaPlayback:)])
        self.webView.allowsInlineMediaPlayback = YES;
    /* scrollView is iOS 5+ — guard so the same binary can be rebuilt for armv6/iOS 3 later */
    if ([self.webView respondsToSelector:@selector(scrollView)]) {
        self.webView.scrollView.bounces = NO;
    }
    [self.view addSubview:self.webView];

    if (self.initialURL) {
        [self.webView loadRequest:[NSURLRequest requestWithURL:self.initialURL]];
    }
}

/* Hide the native nav bar on the tab root page; show it on any sub-page so the
   back button is visible.  Called on appear so it also fires correctly after a pop. */
- (void)viewWillAppear:(BOOL)animated {
    [super viewWillAppear:animated];
    BOOL isRoot = [self.initialURL.absoluteString isEqualToString:self.rootURL.absoluteString];
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
    NSString *accentName = [webView stringByEvaluatingJavaScriptFromString:
        @"(function(){var a=localStorage.getItem('cg-accent-color'); if(!a)a=localStorage.getItem('cg-chat-bubble'); if(!a)a='blue'; return a; })();"];
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
}

#pragma mark - UIWebViewDelegate

- (BOOL)webView:(UIWebView *)webView shouldStartLoadWithRequest:(NSURLRequest *)request
                                                 navigationType:(UIWebViewNavigationType)navigationType {
    NSURL      *url    = request.URL;
    NSString   *scheme = url.scheme.lowercaseString;

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
        next.title = [scheme isEqualToString:@"file"]
                     ? [[url.lastPathComponent stringByDeletingPathExtension] capitalizedString]
                     : url.host;
        [self.navigationController pushViewController:next animated:YES];
        return NO;
    }

    return YES;
}

- (void)webView:(UIWebView *)webView didFailLoadWithError:(NSError *)error {
    /* Ignore normal cancellations (redirect / new navigation started) */
    if (error.code == -999 || error.code == 102) return;
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
