//! Explicit original TRUSTED_PROXY_* configuration. Never resolve request
//! headers as hostnames and never trust an implicit loopback proxy.
use axum::http::HeaderMap;
use ipnet::IpNet;
use std::{collections::HashSet, net::{IpAddr, SocketAddr}, sync::OnceLock, time::{Duration,Instant}};
use tokio::sync::Mutex;

struct ProxyTrust {
    networks: Vec<IpNet>,
    hosts: Vec<String>,
    resolved: Mutex<(Option<Instant>,HashSet<IpAddr>)>,
}
impl ProxyTrust {
    fn new(cidrs:&str, hosts:&str)->Self {
        let networks=cidrs.split(',').take(32).filter_map(|s|{
            let s=s.trim();
            s.parse::<IpNet>().ok().map(|n|n.trunc()).or_else(||s.parse::<IpAddr>().ok().map(IpNet::from))
        }).collect();
        let hosts=hosts.split(',').take(32).filter_map(|s|{
            let name=s.trim().trim_end_matches('.').to_ascii_lowercase();
            (!name.is_empty() && name.len()<=253 &&
             name.bytes().all(|c|c.is_ascii_alphanumeric()||matches!(c,b'-'|b'_'|b'.')))
             .then_some(name)
        }).collect();
        Self{networks,hosts,resolved:Mutex::new((None,HashSet::new()))}
    }
    async fn source(&self,headers:&HeaderMap,addr:Option<SocketAddr>)->String {
        let Some(peer)=addr.map(|a|a.ip()) else{return "unknown".into()};
        let mut resolved=self.resolved.lock().await;
        if resolved.0.is_none_or(|at|at.elapsed()>=Duration::from_secs(60)) {
            let mut addresses=HashSet::new();
            // A fixed bound per refresh, including failed lookups. The
            // shared mutex prevents a login burst from multiplying DNS work.
            let lookups=futures_util::stream::iter(self.hosts.iter().cloned());
            use futures_util::StreamExt;
            let work=lookups.map(|host|async move {
                tokio::net::lookup_host((host.as_str(),0)).await.ok().map(|items|
                    items.take(64).map(|a|a.ip()).filter(|a|!a.is_unspecified()&&!a.is_multicast()).collect::<Vec<_>>())
            }).buffer_unordered(4);
            tokio::pin!(work);
            let _=tokio::time::timeout(Duration::from_secs(2),async {
                while let Some(result)=work.next().await {
                    if let Some(items)=result {addresses.extend(items);}
                }
            }).await;
            *resolved=(Some(Instant::now()),addresses);
        }
        let trusted=|a:IpAddr|resolved.1.contains(&a)||self.networks.iter().any(|n|n.contains(&a));
        source_ip(headers,peer,trusted)
    }
}
fn source_ip(headers:&HeaderMap,peer:IpAddr,trusted:impl Fn(IpAddr)->bool)->String {
    if !trusted(peer){return peer.to_string()}
    // Multiple header lines are ambiguous across proxies: fail closed.
    let mut values=headers.get_all("x-forwarded-for").iter();
    let Some(value)=values.next().and_then(|v|v.to_str().ok()) else{return peer.to_string()};
    if values.next().is_some() || value.len()>4096 {return peer.to_string()}
    let hops:Vec<_>=value.split(',').map(|s|s.trim().parse::<IpAddr>()).collect();
    if hops.len()>64 || hops.iter().any(Result::is_err){return peer.to_string()}
    let mut candidate=peer;
    for hop in hops.into_iter().rev() {
        if !trusted(candidate){break}
        candidate=hop.expect("validated IP");
    }
    candidate.to_string()
}
pub async fn caller_identity(headers:&HeaderMap,addr:Option<SocketAddr>)->String {
    static TRUST:OnceLock<ProxyTrust>=OnceLock::new();
    TRUST.get_or_init(||ProxyTrust::new(
        &std::env::var("TRUSTED_PROXY_CIDRS").unwrap_or_default(),
        &std::env::var("TRUSTED_PROXY_HOSTS").unwrap_or_default()
    )).source(headers,addr).await
}

#[cfg(test)]
mod tests {
    use super::*;
    fn headers(chain:&str)->HeaderMap{
        let mut h=HeaderMap::new();
        h.insert("x-forwarded-for",chain.parse().unwrap());
        h.insert("x-real-ip","1.2.3.4".parse().unwrap());
        h
    }
    async fn addr(trust:&ProxyTrust,chain:&str,peer:&str)->String{
        trust.source(&headers(chain),Some(peer.parse().unwrap())).await
    }
    #[tokio::test]
    async fn explicit_ipv4_ipv6_and_right_to_left() {
        let trust=ProxyTrust::new("127.0.0.1/32,10.1.2.7/24,2001:db8:1::/48","");
        assert_eq!(addr(&trust,"8.8.8.8, 192.0.2.1, 10.1.2.9","127.0.0.1:1234").await,"192.0.2.1");
        assert_eq!(addr(&trust,"2001:db8:2::10, 2001:db8:1::20","[2001:db8:1::30]:1234").await,"2001:db8:2::10");
        assert_eq!(addr(&trust,"8.8.8.8","192.0.2.10:1234").await,"192.0.2.10");
        assert_eq!(addr(&ProxyTrust::new("",""),"8.8.8.8","127.0.0.1:1234").await,"127.0.0.1");
        for invalid in ["8.8.8.8,","hostname,10.1.2.9","8.8.8.8:123",""] {
            assert_eq!(addr(&trust,invalid,"127.0.0.1:1234").await,"127.0.0.1");
        }
        let mut h=headers("8.8.8.8");
        h.append("x-forwarded-for","192.0.2.1".parse().unwrap());
        assert_eq!(trust.source(&h,Some("127.0.0.1:1".parse().unwrap())).await,"127.0.0.1");
    }
    #[tokio::test]
    async fn hostname_refresh_and_untrusted_real_ip() {
        let trust=ProxyTrust::new("","localhost,invalid/name");
        let peer="127.0.0.1:1234";
        assert_eq!(addr(&trust,"192.0.2.1",peer).await,"192.0.2.1");
        {
            let mut cache=trust.resolved.lock().await;
            cache.0=Some(Instant::now()-Duration::from_secs(61));
            cache.1.clear();
        }
        assert_eq!(addr(&trust,"192.0.2.2",peer).await,"192.0.2.2");
        let mut h=HeaderMap::new();
        h.insert("x-real-ip","192.0.2.3".parse().unwrap());
        assert_eq!(trust.source(&h,Some(peer.parse().unwrap())).await,"127.0.0.1");
    }
}
